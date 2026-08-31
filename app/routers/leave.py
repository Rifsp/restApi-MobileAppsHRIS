from datetime import date
from io import BytesIO
from math import ceil
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_identity
from app.schemas.leave import (
    LeaveApplyData,
    LeaveApplyResponse,
    LeaveCancelData,
    LeaveCancelResponse,
    LeaveHistoryData,
    LeaveHistoryItem,
    LeaveHistoryResponse,
    LeaveQuotaData,
    LeaveQuotaResponse,
    LeaveTypeItem,
    LeaveTypesResponse,
)


router = APIRouter(
    prefix="/api/leave",
    tags=["Leave and Permission"],
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEAVE_UPLOAD_ROOT = PROJECT_ROOT / "uploads" / "leave"
MAX_PHOTO_SIZE = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1600

# Department pusat yang sementara melewati FADH.
FADH_DEPARTMENT_IDS = {"PJL", "KEU", "GDG", "DRV"}


def get_employee_context(db: Session, identity):
    employee = db.execute(
        text(
            """
            SELECT
                k.i_karyawan,
                k.i_user,
                k.i_company,
                k.i_store,
                k.i_department,
                k.i_jadwal,
                s.i_store_id,
                s.e_store_name,
                COALESCE(s.f_store_pusat, FALSE) AS f_store_pusat,
                d.i_department_id,
                d.e_department_name
            FROM public.tr_hr_karyawan k
            INNER JOIN public.tr_store s
                ON s.i_store = k.i_store
               AND s.i_company = k.i_company
               AND s.f_store_active = TRUE
            INNER JOIN public.tr_department d
                ON d.i_department = k.i_department
               AND d.f_status = TRUE
            WHERE k.i_karyawan = :i_karyawan
              AND k.i_user = :i_user
              AND k.i_company = :i_company
              AND k.f_aktif = TRUE
            LIMIT 1
            """
        ),
        {
            "i_karyawan": identity["i_karyawan"],
            "i_user": identity["i_user"],
            "i_company": identity["i_company"],
        },
    ).mappings().first()

    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Store atau departemen karyawan belum "
                "dikonfigurasi dengan benar."
            ),
        )

    if employee["i_jadwal"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Jadwal kerja karyawan belum ditentukan.",
        )

    return employee


def get_leave_type(
    db: Session,
    i_company: int,
    i_jenis_izin: int,
):
    leave_type = db.execute(
        text(
            """
            SELECT
                i_jenis_izin,
                i_jenis_id,
                e_jenis_name,
                COALESCE(max_hari, 0) AS max_hari,
                COALESCE(f_butuh_dokumen, FALSE) AS f_butuh_dokumen,
                COALESCE(f_potong_cuti, FALSE) AS f_potong_cuti
            FROM public.tr_hr_jenis_izin
            WHERE i_jenis_izin = :i_jenis_izin
              AND i_company = :i_company
              AND f_aktif = TRUE
              AND i_jenis_id IN ('CUTI', 'IZIN')
            LIMIT 1
            """
        ),
        {
            "i_jenis_izin": i_jenis_izin,
            "i_company": i_company,
        },
    ).mappings().first()

    if leave_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jenis pengajuan tidak ditemukan atau tidak aktif.",
        )

    return leave_type


def calculate_working_days(
    db: Session,
    i_company: int,
    i_jadwal: int,
    start_date: date,
    end_date: date,
) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT COUNT(*)::integer
                FROM generate_series(
                    CAST(:d_mulai AS date),
                    CAST(:d_selesai AS date),
                    interval '1 day'
                ) AS x(d_tanggal)
                INNER JOIN public.tr_hr_jadwal_hari h
                    ON h.i_jadwal = :i_jadwal
                   AND h.i_company = :i_company
                   AND h.n_hari = EXTRACT(DOW FROM x.d_tanggal)::integer
                   AND COALESCE(h.f_libur, FALSE) = FALSE
                LEFT JOIN public.tr_hr_hari_libur l
                    ON l.i_company = :i_company
                   AND l.d_libur = x.d_tanggal::date
                WHERE l.i_libur IS NULL
                """
            ),
            {
                "d_mulai": start_date,
                "d_selesai": end_date,
                "i_jadwal": i_jadwal,
                "i_company": i_company,
            },
        ).scalar_one()
    )


def validate_leave_quota(
    db: Session,
    i_company: int,
    i_user: int,
    year: int,
    required_days: int,
) -> None:
    quota = db.execute(
        text(
            """
            SELECT n_sisa, d_expired
            FROM public.tm_hr_jatah_cuti
            WHERE i_company = :i_company
              AND i_user = :i_user
              AND i_tahun = :i_tahun
            LIMIT 1
            """
        ),
        {
            "i_company": i_company,
            "i_user": i_user,
            "i_tahun": year,
        },
    ).mappings().first()

    if quota is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Jatah cuti tahun {year} belum tersedia.",
        )

    if quota["d_expired"] is not None and quota["d_expired"] < date.today():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Jatah cuti tahun {year} sudah kedaluwarsa.",
        )

    if int(quota["n_sisa"]) < required_days:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Sisa cuti hanya {quota['n_sisa']} hari, "
                f"sedangkan pengajuan membutuhkan {required_days} hari."
            ),
        )


async def prepare_leave_photo(photo: UploadFile) -> tuple[bytes, str]:
    content_type = (photo.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lampiran izin harus berupa JPG atau PNG.",
        )

    photo_bytes = await photo.read(MAX_PHOTO_SIZE + 1)
    if not photo_bytes or len(photo_bytes) > MAX_PHOTO_SIZE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Ukuran lampiran izin maksimal 5 MB.",
        )

    try:
        source = Image.open(BytesIO(photo_bytes))
        source.verify()
        source = Image.open(BytesIO(photo_bytes))
        source = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Isi lampiran bukan gambar yang valid.",
        )

    source.thumbnail(
        (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
        Image.Resampling.LANCZOS,
    )
    output = BytesIO()
    source.save(output, format="JPEG", quality=85, optimize=True)
    return output.getvalue(), ".jpg"


def initial_approval_status(employee) -> int:
    if employee["f_store_pusat"] is not True:
        return 5

    department_id = str(employee["i_department_id"] or "").upper()
    return 4 if department_id in FADH_DEPARTMENT_IDS else 5


def status_name(row) -> str:
    if row["f_cancel"] is True:
        return "DIBATALKAN"
    if row["f_reject"] is True:
        return "DITOLAK"
    return row["e_status_dn_name"]


@router.get("/types", response_model=LeaveTypesResponse)
def get_leave_types(
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text(
            """
            SELECT
                i_jenis_izin,
                i_jenis_id,
                e_jenis_name,
                COALESCE(max_hari, 0) AS max_hari,
                COALESCE(f_butuh_dokumen, FALSE) AS f_butuh_dokumen,
                COALESCE(f_potong_cuti, FALSE) AS f_potong_cuti
            FROM public.tr_hr_jenis_izin
            WHERE i_company = :i_company
              AND f_aktif = TRUE
              AND i_jenis_id IN ('CUTI', 'IZIN')
            ORDER BY i_jenis_id
            """
        ),
        {"i_company": identity["i_company"]},
    ).mappings().all()

    return LeaveTypesResponse(
        status=True,
        message="Jenis pengajuan berhasil dimuat.",
        data=[LeaveTypeItem(**row) for row in rows],
    )


@router.get("/quota", response_model=LeaveQuotaResponse)
def get_leave_quota(
    year: int | None = Query(None, ge=2000, le=2100),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    year = year or date.today().year
    row = db.execute(
        text(
            """
            SELECT
                i_tahun,
                n_jatah,
                COALESCE(n_diambil, 0) AS n_diambil,
                n_sisa,
                d_expired
            FROM public.tm_hr_jatah_cuti
            WHERE i_company = :i_company
              AND i_user = :i_user
              AND i_tahun = :i_tahun
            LIMIT 1
            """
        ),
        {
            "i_company": identity["i_company"],
            "i_user": identity["i_user"],
            "i_tahun": year,
        },
    ).mappings().first()

    return LeaveQuotaResponse(
        status=True,
        message=(
            "Jatah cuti berhasil dimuat."
            if row is not None
            else f"Jatah cuti tahun {year} belum tersedia."
        ),
        data=LeaveQuotaData(**row) if row is not None else None,
    )


@router.get("/history", response_model=LeaveHistoryResponse)
def get_leave_history(
    year: int | None = Query(None, ge=2000, le=2100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    year = year or date.today().year
    parameters = {
        "i_company": identity["i_company"],
        "i_user": identity["i_user"],
        "i_tahun": year,
    }
    total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)::integer
                FROM public.tm_hr_pengajuan_izin
                WHERE i_company = :i_company
                  AND i_user = :i_user
                  AND EXTRACT(YEAR FROM d_pengajuan)::integer = :i_tahun
                """
            ),
            parameters,
        ).scalar_one()
    )

    rows = db.execute(
        text(
            """
            SELECT
                p.i_pengajuan,
                p.i_pengajuan_id,
                p.i_jenis_izin,
                j.i_jenis_id,
                j.e_jenis_name,
                p.d_pengajuan,
                p.d_mulai,
                p.d_selesai,
                p.n_hari,
                p.e_alasan,
                p.e_lampiran,
                p.i_status_dn,
                s.e_status_dn_name,
                p.f_reject,
                p.e_reject_reason,
                p.f_cancel,
                p.e_cancel_reason,
                (
                    p.f_reject = FALSE
                    AND p.f_cancel = FALSE
                    AND p.i_user_acc3 IS NULL
                    AND p.i_user_acc4 IS NULL
                    AND p.i_user_acc5 IS NULL
                ) AS can_cancel,
                p.d_entry
            FROM public.tm_hr_pengajuan_izin p
            INNER JOIN public.tr_hr_jenis_izin j
                ON j.i_jenis_izin = p.i_jenis_izin
               AND j.i_company = p.i_company
            INNER JOIN public.tr_status_dn s
                ON s.i_status_dn = p.i_status_dn
            WHERE p.i_company = :i_company
              AND p.i_user = :i_user
              AND EXTRACT(YEAR FROM p.d_pengajuan)::integer = :i_tahun
            ORDER BY p.d_entry DESC, p.i_pengajuan DESC
            LIMIT :page_size OFFSET :offset
            """
        ),
        {
            **parameters,
            "page_size": page_size,
            "offset": (page - 1) * page_size,
        },
    ).mappings().all()

    items = []
    for row in rows:
        item = dict(row)
        item["e_status"] = status_name(row)
        item.pop("e_status_dn_name")
        items.append(LeaveHistoryItem(**item))

    return LeaveHistoryResponse(
        status=True,
        message="Riwayat pengajuan berhasil dimuat.",
        data=LeaveHistoryData(
            year=year,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=ceil(total / page_size) if total else 0,
            items=items,
        ),
    )


@router.post("/apply", response_model=LeaveApplyResponse)
async def apply_leave(
    i_jenis_izin: int = Form(..., gt=0),
    d_mulai: date = Form(...),
    d_selesai: date = Form(...),
    e_alasan: str = Form(..., min_length=3, max_length=1000),
    photo: UploadFile | None = File(None),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    if d_selesai < d_mulai:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tanggal selesai tidak boleh sebelum tanggal mulai.",
        )

    reason = e_alasan.strip()
    if len(reason) < 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Alasan pengajuan minimal 3 karakter.",
        )

    employee = get_employee_context(db, identity)
    leave_type = get_leave_type(db, identity["i_company"], i_jenis_izin)

    if leave_type["f_potong_cuti"] is True and d_mulai.year != d_selesai.year:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pengajuan cuti tidak boleh melewati pergantian tahun.",
        )

    working_days = calculate_working_days(
        db,
        identity["i_company"],
        employee["i_jadwal"],
        d_mulai,
        d_selesai,
    )
    if working_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Periode pengajuan tidak memiliki hari kerja.",
        )

    max_days = int(leave_type["max_hari"] or 0)
    if max_days > 0 and working_days > max_days:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Maksimal pengajuan adalah {max_days} hari kerja.",
        )

    if leave_type["f_potong_cuti"] is True:
        validate_leave_quota(
            db,
            identity["i_company"],
            identity["i_user"],
            d_mulai.year,
            working_days,
        )

    overlap = db.execute(
        text(
            """
            SELECT i_pengajuan_id
            FROM public.tm_hr_pengajuan_izin
            WHERE i_company = :i_company
              AND i_user = :i_user
              AND f_reject = FALSE
              AND f_cancel = FALSE
              AND daterange(d_mulai, d_selesai, '[]')
                  && daterange(
                        CAST(:d_mulai AS date),
                        CAST(:d_selesai AS date),
                        '[]'
                     )
            LIMIT 1
            """
        ),
        {
            "i_company": identity["i_company"],
            "i_user": identity["i_user"],
            "d_mulai": d_mulai,
            "d_selesai": d_selesai,
        },
    ).first()
    if overlap is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Periode bertabrakan dengan pengajuan {overlap[0]}.",
        )

    photo_path = None
    saved_file = None
    if leave_type["f_butuh_dokumen"] is True:
        if photo is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Foto lampiran wajib untuk pengajuan izin.",
            )
        photo_bytes, extension = await prepare_leave_photo(photo)
        upload_date = date.today()
        relative_directory = Path(
            "leave",
            str(upload_date.year),
            f"{upload_date.month:02d}",
            f"{upload_date.day:02d}",
        )
        target_directory = PROJECT_ROOT / "uploads" / relative_directory
        target_directory.mkdir(parents=True, exist_ok=True)
        filename = f"leave_{identity['i_user']}_{uuid4().hex}{extension}"
        saved_file = target_directory / filename
        saved_file.write_bytes(photo_bytes)
        photo_path = (relative_directory / filename).as_posix()
    elif photo is not None:
        await photo.close()

    approval_status = initial_approval_status(employee)
    application_id = (
        f"PGJ-{date.today():%Y%m%d}-"
        f"{identity['i_company']}-{uuid4().hex[:10].upper()}"
    )

    try:
        row = db.execute(
            text(
                """
                INSERT INTO public.tm_hr_pengajuan_izin (
                    i_company,
                    i_pengajuan_id,
                    i_karyawan,
                    i_user,
                    i_store,
                    i_department,
                    i_jenis_izin,
                    d_pengajuan,
                    d_mulai,
                    d_selesai,
                    n_hari,
                    e_alasan,
                    e_lampiran,
                    i_status_dn
                ) VALUES (
                    :i_company,
                    :i_pengajuan_id,
                    :i_karyawan,
                    :i_user,
                    :i_store,
                    :i_department,
                    :i_jenis_izin,
                    CURRENT_DATE,
                    :d_mulai,
                    :d_selesai,
                    :n_hari,
                    :e_alasan,
                    :e_lampiran,
                    :i_status_dn
                )
                RETURNING i_pengajuan, i_pengajuan_id, i_status_dn
                """
            ),
            {
                "i_company": identity["i_company"],
                "i_pengajuan_id": application_id,
                "i_karyawan": identity["i_karyawan"],
                "i_user": identity["i_user"],
                "i_store": employee["i_store"],
                "i_department": employee["i_department"],
                "i_jenis_izin": i_jenis_izin,
                "d_mulai": d_mulai,
                "d_selesai": d_selesai,
                "n_hari": working_days,
                "e_alasan": reason,
                "e_lampiran": photo_path,
                "i_status_dn": approval_status,
            },
        ).mappings().one()
        db.commit()
    except (IntegrityError, SQLAlchemyError):
        db.rollback()
        if saved_file is not None:
            saved_file.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pengajuan gagal disimpan. Periksa log server.",
        )
    finally:
        if photo is not None:
            await photo.close()

    status_label = "Menunggu Approve FADH" if approval_status == 4 else "Menunggu Approve GM"
    return LeaveApplyResponse(
        status=True,
        message="Pengajuan berhasil dikirim.",
        data=LeaveApplyData(
            **row,
            e_status=status_label,
            n_hari=working_days,
            e_lampiran=photo_path,
        ),
    )


@router.post("/{i_pengajuan}/cancel", response_model=LeaveCancelResponse)
def cancel_leave(
    i_pengajuan: int,
    reason: str | None = Form(None, max_length=1000),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    try:
        row = db.execute(
            text(
                """
                SELECT
                    i_pengajuan,
                    i_pengajuan_id,
                    f_reject,
                    f_cancel,
                    i_user_acc3,
                    i_user_acc4,
                    i_user_acc5
                FROM public.tm_hr_pengajuan_izin
                WHERE i_pengajuan = :i_pengajuan
                  AND i_company = :i_company
                  AND i_user = :i_user
                FOR UPDATE
                """
            ),
            {
                "i_pengajuan": i_pengajuan,
                "i_company": identity["i_company"],
                "i_user": identity["i_user"],
            },
        ).mappings().first()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pengajuan tidak ditemukan.",
            )
        if row["f_reject"] is True or row["f_cancel"] is True:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pengajuan sudah ditolak atau dibatalkan.",
            )
        if any(row[key] is not None for key in ("i_user_acc3", "i_user_acc4", "i_user_acc5")):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pengajuan yang sudah memperoleh approval tidak dapat dibatalkan.",
            )

        db.execute(
            text(
                """
                UPDATE public.tm_hr_pengajuan_izin
                SET f_cancel = TRUE,
                    i_user_cancel = :i_user,
                    d_cancel = CURRENT_TIMESTAMP,
                    e_cancel_reason = :reason,
                    d_update = CURRENT_TIMESTAMP
                WHERE i_pengajuan = :i_pengajuan
                """
            ),
            {
                "i_user": identity["i_user"],
                "reason": (reason or "").strip() or None,
                "i_pengajuan": i_pengajuan,
            },
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pengajuan gagal dibatalkan. Periksa log server.",
        )

    return LeaveCancelResponse(
        status=True,
        message="Pengajuan berhasil dibatalkan.",
        data=LeaveCancelData(
            i_pengajuan=row["i_pengajuan"],
            i_pengajuan_id=row["i_pengajuan_id"],
            f_cancel=True,
        ),
    )
