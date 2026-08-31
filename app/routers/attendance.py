from io import BytesIO
from math import atan2, cos, radians, sin, sqrt
from math import ceil
from pathlib import Path
from textwrap import wrap

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
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from timezonefinder import TimezoneFinder

from app.database import get_db
from app.dependencies.auth import get_current_identity
from app.schemas.attendance import (
    ActiveDinasData,
    AttendanceContextData,
    AttendanceContextResponse,
    AttendanceHistoryData,
    AttendanceHistoryItem,
    AttendanceHistoryResponse,
    CheckInData,
    CheckInResponse,
    CheckOutData,
    CheckOutResponse,
)


router = APIRouter(
    prefix="/api/attendance",
    tags=["Attendance"],
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ATTENDANCE_UPLOAD_ROOT = PROJECT_ROOT / "uploads" / "attendance"
MAX_PHOTO_SIZE = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1600
MAX_GPS_ACCURACY_METER = 50
TIMEZONE_FINDER = TimezoneFinder(in_memory=True)

TIMEZONE_LABELS = {
    "Asia/Jakarta": "WIB",
    "Asia/Makassar": "WITA",
    "Asia/Jayapura": "WIT",
}


def validate_gps_security(
    accuracy: float,
    is_mocked: bool,
) -> None:
    if accuracy <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Akurasi GPS tidak valid.",
        )

    if accuracy > MAX_GPS_ACCURACY_METER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Akurasi GPS {round(accuracy)} meter. "
                f"Maksimal {MAX_GPS_ACCURACY_METER} meter."
            ),
        )

    if is_mocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Lokasi palsu terdeteksi. "
                "Matikan aplikasi Fake GPS lalu coba kembali."
            ),
        )


def calculate_distance_meter(
    latitude_1: float,
    longitude_1: float,
    latitude_2: float,
    longitude_2: float,
) -> float:
    earth_radius_meter = 6_371_000
    latitude_difference = radians(latitude_2 - latitude_1)
    longitude_difference = radians(longitude_2 - longitude_1)

    calculation = (
        sin(latitude_difference / 2) ** 2
        + cos(radians(latitude_1))
        * cos(radians(latitude_2))
        * sin(longitude_difference / 2) ** 2
    )

    return earth_radius_meter * 2 * atan2(
        sqrt(calculation),
        sqrt(1 - calculation),
    )


def get_active_dinas(
    db: Session,
    i_karyawan: int,
    i_company: int,
    attendance_date,
):
    rows = db.execute(
        text(
            """
            SELECT
                d.i_dinas,
                d.i_dinas_id,

                CASE
                    WHEN COALESCE(d.f_new, FALSE) = TRUE
                        AND NULLIF(
                            btrim(d.e_kota),
                            ''
                        ) IS NOT NULL
                        AND d.e_kota ~
                            '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                        AND d.e_area ~
                            '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                    THEN (
                        SELECT string_agg(
                            initcap(c.e_city_name),
                            ', '
                            ORDER BY c.e_city_name
                        )
                        FROM public.tr_city c
                        WHERE c.i_city = ANY (
                            regexp_split_to_array(
                                btrim(d.e_kota),
                                '\\s*,\\s*'
                            )::integer[]
                        )
                        AND c.i_area = ANY (
                            regexp_split_to_array(
                                btrim(d.e_area),
                                '\\s*,\\s*'
                            )::integer[]
                        )
                    )
                    ELSE d.e_kota
                END AS e_kota,

                CASE
                    WHEN COALESCE(d.f_new, FALSE) = TRUE
                        AND NULLIF(
                            btrim(d.e_area),
                            ''
                        ) IS NOT NULL
                        AND d.e_area ~
                            '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                    THEN (
                        SELECT string_agg(
                            initcap(a.e_area_name),
                            ', '
                            ORDER BY a.e_area_name
                        )
                        FROM public.tr_area a
                        WHERE a.i_area = ANY (
                            regexp_split_to_array(
                                btrim(d.e_area),
                                '\\s*,\\s*'
                            )::integer[]
                        )
                    )
                    ELSE d.e_area
                END AS e_area,

                d.e_remark,
                to_char(
                    d.d_berangkat,
                    'YYYY-MM-DD'
                ) AS d_berangkat,
                to_char(
                    d.d_kembali,
                    'YYYY-MM-DD'
                ) AS d_kembali
            FROM public.tm_dinas d
            WHERE d.i_karyawan = :i_karyawan
              AND d.i_company = :i_company
              AND d.i_status_dn = 6
              AND COALESCE(
                    d.f_dinas_cancel,
                    FALSE
              ) = FALSE
              AND d.d_dcc IS NULL
              AND CAST(:attendance_date AS date)
                    BETWEEN d.d_berangkat
                    AND d.d_kembali
            ORDER BY d.d_berangkat DESC, d.i_dinas DESC
            """
        ),
        {
            "i_karyawan": i_karyawan,
            "i_company": i_company,
            "attendance_date": attendance_date,
        },
    ).mappings().all()

    if len(rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Ditemukan lebih dari satu penugasan "
                "dinas aktif pada tanggal ini. Hubungi HRD."
            ),
        )

    return rows[0] if rows else None


def get_company_holiday(
    db: Session,
    i_company: int,
    attendance_date,
):
    return db.execute(
        text(
            """
            SELECT
                i_libur,
                d_libur,
                e_keterangan,
                COALESCE(f_nasional, FALSE) AS f_nasional
            FROM public.tr_hr_hari_libur
            WHERE i_company = :i_company
            AND d_libur = CAST(:attendance_date AS date)
            LIMIT 1
            """
        ),
        {
            "i_company": i_company,
            "attendance_date": attendance_date,
        },
    ).mappings().first()


@router.get(
    "/context",
    response_model=AttendanceContextResponse,
)
def get_attendance_context(
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    employee_timezone = (
        identity["e_timezone"]
        or "Asia/Jakarta"
    )

    attendance_date = db.execute(
        text(
            """
            SELECT (
                CURRENT_TIMESTAMP
                AT TIME ZONE :e_timezone
            )::date
            """
        ),
        {
            "e_timezone": employee_timezone,
        },
    ).scalar_one()

    active_dinas = get_active_dinas(
        db=db,
        i_karyawan=identity["i_karyawan"],
        i_company=identity["i_company"],
        attendance_date=attendance_date,
    )

    return AttendanceContextResponse(
        status=True,
        message="Konteks absensi berhasil dimuat.",
        data=AttendanceContextData(
            tanggal=attendance_date.isoformat(),
            has_active_dinas=active_dinas is not None,
            active_dinas=(
                ActiveDinasData(**active_dinas)
                if active_dinas is not None
                else None
            ),
        ),
    )


@router.get(
    "/history",
    response_model=AttendanceHistoryResponse,
)
def get_attendance_history(
    year: int | None = Query(default=None, ge=2020, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    employee_timezone = (
        identity["e_timezone"]
        or "Asia/Jakarta"
    )

    current_period = db.execute(
        text(
            """
            SELECT
                EXTRACT(
                    YEAR FROM CURRENT_TIMESTAMP
                    AT TIME ZONE :e_timezone
                )::integer AS current_year,
                EXTRACT(
                    MONTH FROM CURRENT_TIMESTAMP
                    AT TIME ZONE :e_timezone
                )::integer AS current_month
            """
        ),
        {
            "e_timezone": employee_timezone,
        },
    ).mappings().one()

    selected_year = year or current_period["current_year"]
    selected_month = month or current_period["current_month"]
    period = f"{selected_year:04d}-{selected_month:02d}"

    total = db.execute(
        text(
            """
            SELECT COUNT(*)::integer
            FROM public.tm_hr_absensi a
            WHERE a.i_user = :i_user
              AND a.i_company = :i_company
              AND a.d_absen >= make_date(
                    :selected_year,
                    :selected_month,
                    1
              )
              AND a.d_absen < (
                    make_date(
                        :selected_year,
                        :selected_month,
                        1
                    ) + INTERVAL '1 month'
              )
            """
        ),
        {
            "i_user": identity["i_user"],
            "i_company": identity["i_company"],
            "selected_year": selected_year,
            "selected_month": selected_month,
        },
    ).scalar_one()

    offset = (page - 1) * page_size

    rows = db.execute(
        text(
            """
            SELECT
                a.i_absensi,
                to_char(
                    a.d_absen,
                    'YYYY-MM-DD'
                ) AS tanggal,
                to_char(
                    a.jam_in,
                    'HH24:MI:SS'
                ) AS jam_in,
                to_char(
                    a.jam_out,
                    'HH24:MI:SS'
                ) AS jam_out,
                COALESCE(
                    a.e_status,
                    'HADIR'
                ) AS e_status,
                COALESCE(
                    a.menit_terlambat,
                    0
                ) AS menit_terlambat,
                COALESCE(
                    a.menit_lembur,
                    0
                ) AS menit_lembur,
                a.metode_in,
                a.metode_out,
                l.e_lokasi_name,
                a.alamat_in,
                a.alamat_out,
                a.foto_in,
                a.foto_out,
                COALESCE(
                    a.e_timezone,
                    :employee_timezone
                ) AS e_timezone,
                (a.i_dinas IS NOT NULL) AS is_lk,
                a.i_dinas,
                d.i_dinas_id,

                CASE
                    WHEN COALESCE(d.f_new, FALSE) = TRUE
                         AND NULLIF(
                            btrim(d.e_kota),
                            ''
                         ) IS NOT NULL
                         AND d.e_kota ~
                            '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                         AND d.e_area ~
                            '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                    THEN (
                        SELECT string_agg(
                            initcap(c.e_city_name),
                            ', '
                            ORDER BY c.e_city_name
                        )
                        FROM public.tr_city c
                        WHERE c.i_city = ANY (
                            regexp_split_to_array(
                                btrim(d.e_kota),
                                '\\s*,\\s*'
                            )::integer[]
                        )
                          AND c.i_area = ANY (
                            regexp_split_to_array(
                                btrim(d.e_area),
                                '\\s*,\\s*'
                            )::integer[]
                        )
                    )
                    ELSE d.e_kota
                END AS e_kota
            FROM public.tm_hr_absensi a
            LEFT JOIN public.tr_hr_lokasi_absen l
                ON l.i_lokasi = a.i_lokasi
                AND l.i_company = a.i_company
            LEFT JOIN public.tm_dinas d
                ON d.i_dinas = a.i_dinas
                AND d.i_company = a.i_company
            WHERE a.i_user = :i_user
              AND a.i_company = :i_company
              AND a.d_absen >= make_date(
                    :selected_year,
                    :selected_month,
                    1
              )
              AND a.d_absen < (
                    make_date(
                        :selected_year,
                        :selected_month,
                        1
                    ) + INTERVAL '1 month'
              )
            ORDER BY a.d_absen DESC, a.i_absensi DESC
            LIMIT :page_size
            OFFSET :offset
            """
        ),
        {
            "i_user": identity["i_user"],
            "i_company": identity["i_company"],
            "selected_year": selected_year,
            "selected_month": selected_month,
            "employee_timezone": employee_timezone,
            "page_size": page_size,
            "offset": offset,
        },
    ).mappings().all()

    return AttendanceHistoryResponse(
        status=True,
        message="Riwayat absensi berhasil dimuat.",
        data=AttendanceHistoryData(
            period=period,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=ceil(total / page_size) if total else 0,
            items=[
                AttendanceHistoryItem(**row)
                for row in rows
            ],
        ),
    )


def determine_check_in_timezone(
    latitude: float,
    longitude: float,
    identity,
) -> str:
    base_timezone = (
        identity["e_timezone"]
        or "Asia/Jakarta"
    )

    if identity["f_wajib_lokasi"] is True:
        return base_timezone

    detected_timezone = TIMEZONE_FINDER.timezone_at(
        lat=latitude,
        lng=longitude,
    )

    return detected_timezone or base_timezone


def get_timezone_label(timezone_name: str) -> str:
    return TIMEZONE_LABELS.get(
        timezone_name,
        timezone_name,
    )


def get_watermark_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for font_path in font_candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size)

    return ImageFont.load_default()


def save_attendance_photo(
    photo_bytes: bytes,
    attendance_type: str,
    i_user: int,
    attendance_time,
    employee_name: str,
    location_name: str,
    address: str,
    latitude: float,
    longitude: float,
    attendance_timezone: str,
    dinas_id: str | None = None,
    dinas_city: str | None = None,
) -> tuple[str, Path]:
    try:
        source_image = Image.open(BytesIO(photo_bytes))
        source_image.verify()

        source_image = Image.open(BytesIO(photo_bytes))
        source_image = ImageOps.exif_transpose(source_image).convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File foto tidak valid atau rusak.",
        ) from error

    source_image.thumbnail(
        (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
        Image.Resampling.LANCZOS,
    )

    image_width, image_height = source_image.size
    font_size = max(18, image_width // 40)
    font = get_watermark_font(font_size)
    bold_font = get_watermark_font(font_size + 3)
    horizontal_padding = max(18, image_width // 45)
    vertical_padding = max(14, image_height // 70)
    line_spacing = max(6, font_size // 3)

    address_lines = wrap(address, width=55) or ["Alamat tidak tersedia"]
    attendance_label = (
        "ABSEN MASUK"
        if attendance_type == "in"
        else "ABSEN PULANG"
    )

    if dinas_id is not None:
        attendance_label += " - DINAS LUAR KOTA"

    watermark_lines = [
        (attendance_label, bold_font),
        *(
            [
                (f"No. Dinas: {dinas_id}", font),
                (f"Tujuan: {dinas_city or '-'}", font),
            ]
            if dinas_id is not None
            else []
        ),
        (employee_name, font),
        (
            attendance_time.strftime("%d-%m-%Y %H:%M:%S")
            + " "
            + get_timezone_label(attendance_timezone),
            font,
        ),
        (location_name, font),
        *[(line, font) for line in address_lines[:3]],
        (f"Lat: {latitude:.7f} | Long: {longitude:.7f}", font),
    ]

    draw = ImageDraw.Draw(source_image, "RGBA")
    measured_lines = []
    watermark_height = vertical_padding * 2

    for line, line_font in watermark_lines:
        bounds = draw.textbbox((0, 0), line, font=line_font)
        line_height = bounds[3] - bounds[1]
        measured_lines.append((line, line_font, line_height))
        watermark_height += line_height + line_spacing

    watermark_height -= line_spacing
    watermark_top = max(0, image_height - watermark_height)

    draw.rectangle(
        (0, watermark_top, image_width, image_height),
        fill=(0, 0, 0, 165),
    )

    text_y = watermark_top + vertical_padding

    for line, line_font, line_height in measured_lines:
        draw.text(
            (horizontal_padding, text_y),
            line,
            font=line_font,
            fill=(255, 255, 255, 255),
        )
        text_y += line_height + line_spacing

    date_directory = attendance_time.strftime("%Y/%m/%d")
    filename = (
        f"{attendance_type}_{i_user}_"
        f"{attendance_time.strftime('%Y%m%d_%H%M%S')}.jpg"
    )
    relative_path = f"attendance/{date_directory}/{filename}"
    destination_directory = ATTENDANCE_UPLOAD_ROOT / date_directory
    destination_path = destination_directory / filename

    destination_directory.mkdir(parents=True, exist_ok=True)
    source_image.save(
        destination_path,
        format="JPEG",
        quality=88,
        optimize=True,
    )

    return relative_path, destination_path


@router.post(
    "/check-in",
    response_model=CheckInResponse,
    status_code=status.HTTP_201_CREATED,
)
async def check_in(
    latitude: float = Form(..., ge=-90, le=90),
    longitude: float = Form(..., ge=-180, le=180),
    address: str = Form(..., min_length=3, max_length=1000),
    accuracy: float = Form(..., gt=0),
    is_mocked: bool = Form(False),
    attendance_mode: str = Form("REGULAR"),
    photo: UploadFile = File(...),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    validate_gps_security(
        accuracy=accuracy,
        is_mocked=is_mocked,
    )

    if identity["i_jadwal"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Jadwal kerja karyawan belum ditentukan.",
        )

    normalized_mode = attendance_mode.strip().upper()

    if normalized_mode not in {"REGULAR", "LK"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="attendance_mode harus REGULAR atau LK.",
        )

    base_timezone = (
        identity["e_timezone"]
        or "Asia/Jakarta"
    )

    base_attendance_date = db.execute(
        text(
            """
            SELECT (
                CURRENT_TIMESTAMP
                AT TIME ZONE :e_timezone
            )::date
            """
        ),
        {
            "e_timezone": base_timezone,
        },
    ).scalar_one()

    holiday = get_company_holiday(
        db=db,
        i_company=identity["i_company"],
        attendance_date=base_attendance_date,
    )

    active_dinas = None

    if normalized_mode == "LK":
        active_dinas = get_active_dinas(
            db=db,
            i_karyawan=identity["i_karyawan"],
            i_company=identity["i_company"],
            attendance_date=base_attendance_date,
        )

        if active_dinas is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Anda tidak memiliki penugasan dinas "
                    "luar kota yang aktif pada tanggal ini."
                ),
            )

    attendance_timezone = determine_check_in_timezone(
        latitude=latitude,
        longitude=longitude,
        identity=identity,
    )

    schedule = db.execute(
        text(
            """
            SELECT
                (
                    CURRENT_TIMESTAMP
                    AT TIME ZONE :e_timezone
                ) AS waktu_sekarang,
                h.jam_masuk,
                h.jam_keluar,
                COALESCE(h.toleransi_menit, 0) AS toleransi_menit,
                COALESCE(h.f_libur, FALSE) AS f_libur
            FROM public.tr_hr_jadwal_hari h
            WHERE h.i_jadwal = :i_jadwal
              AND h.i_company = :i_company
              AND h.n_hari = EXTRACT(
                    DOW FROM
                    CURRENT_TIMESTAMP
                    AT TIME ZONE :e_timezone
              )::integer
            LIMIT 1
            """
        ),
        {
            "i_jadwal": identity["i_jadwal"],
            "i_company": identity["i_company"],
            "e_timezone": attendance_timezone,
        },
    ).mappings().first()

    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal kerja hari ini belum tersedia.",
        )

    is_holiday = (
        schedule["f_libur"] is True
        or holiday is not None
    )

    if (
        is_holiday
        and normalized_mode == "REGULAR"
        and identity["f_wajib_lokasi"] is True
    ):
        holiday_description = (
            holiday["e_keterangan"]
            if holiday is not None
            else "Libur jadwal kerja"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Hari ini libur: {holiday_description}. "
                "Absensi reguler tidak tersedia."
            ),
        )

    if schedule["jam_masuk"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Jam masuk hari ini belum dikonfigurasi.",
        )

    existing_attendance = db.execute(
        text(
            """
            SELECT i_absensi
            FROM public.tm_hr_absensi
            WHERE i_company = :i_company
              AND i_user = :i_user
              AND d_absen = :attendance_date
            LIMIT 1
            """
        ),
        {
            "i_company": identity["i_company"],
            "i_user": identity["i_user"],
            "attendance_date": schedule["waktu_sekarang"].date(),
        },
    ).first()

    if existing_attendance is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Anda sudah melakukan absen masuk hari ini.",
        )

    location = None
    distance_meter = None

    if identity["i_lokasi_absen"] is not None:
        location = db.execute(
            text(
                """
                SELECT
                    i_lokasi,
                    e_lokasi_name,
                    latitude,
                    longitude,
                    COALESCE(radius_meter, 100) AS radius_meter
                FROM public.tr_hr_lokasi_absen
                WHERE i_lokasi = :i_lokasi
                  AND i_company = :i_company
                  AND f_aktif = TRUE
                LIMIT 1
                """
            ),
            {
                "i_lokasi": identity["i_lokasi_absen"],
                "i_company": identity["i_company"],
            },
        ).mappings().first()

    if (
        normalized_mode == "REGULAR"
        and identity["f_wajib_lokasi"] is True
        and location is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lokasi absensi karyawan belum dikonfigurasi.",
        )

    if location is not None:
        distance_meter = calculate_distance_meter(
            latitude,
            longitude,
            float(location["latitude"]),
            float(location["longitude"]),
        )

        if (
            normalized_mode == "REGULAR"
            and
            identity["f_wajib_lokasi"] is True
            and distance_meter > int(location["radius_meter"])
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Anda berada di luar radius absensi. "
                    f"Jarak {round(distance_meter)} meter, "
                    f"radius maksimal {location['radius_meter']} meter."
                ),
            )

    if photo.content_type not in {"image/jpeg", "image/jpg"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Foto harus menggunakan format JPG.",
        )

    photo_bytes = await photo.read(MAX_PHOTO_SIZE + 1)
    await photo.close()

    if len(photo_bytes) > MAX_PHOTO_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Ukuran foto maksimal 5 MB.",
        )

    relative_photo_path, saved_photo_path = save_attendance_photo(
        photo_bytes=photo_bytes,
        attendance_type="in",
        i_user=identity["i_user"],
        attendance_time=schedule["waktu_sekarang"],
        employee_name=(
            identity["e_karyawan_name"]
            or identity["e_user_name"]
        ),
        location_name=(
            f"Dinas LK - {active_dinas['e_kota'] or '-'}"
            if active_dinas is not None
            else (
                location["e_lokasi_name"]
                if location is not None
                else "Lokasi fleksibel"
            )
        ),
        address=address.strip(),
        latitude=latitude,
        longitude=longitude,
        attendance_timezone=attendance_timezone,
        dinas_id=(
            active_dinas["i_dinas_id"]
            if active_dinas is not None
            else None
        ),
        dinas_city=(
            active_dinas["e_kota"]
            if active_dinas is not None
            else None
        ),
    )

    try:
        attendance = db.execute(
            text(
                """
                INSERT INTO public.tm_hr_absensi (
                    i_company,
                    i_user,
                    i_jadwal,
                    i_lokasi,
                    i_dinas,
                    d_absen,
                    jam_in,
                    metode_in,
                    latitude_in,
                    longitude_in,
                    alamat_in,
                    foto_in,
                    e_timezone,
                    e_status,
                    menit_terlambat
                )
                VALUES (
                    :i_company,
                    :i_user,
                    :i_jadwal,
                    :i_lokasi,
                    :i_dinas,
                    CAST(:attendance_time AS timestamp)::date,
                    :attendance_time,
                    'GPS',
                    :latitude,
                    :longitude,
                    :address,
                    :foto_in,
                    :e_timezone,
                    CASE
                        WHEN :attendance_time >
                             (
                                 CAST(:attendance_time AS timestamp)::date
                                 + :jam_masuk
                                 + (
                                     :toleransi_menit
                                     * INTERVAL '1 minute'
                                 )
                             )
                        THEN 'TERLAMBAT'
                        ELSE 'HADIR'
                    END,
                    CASE
                        WHEN :attendance_time >
                             (
                                 CAST(:attendance_time AS timestamp)::date
                                 + :jam_masuk
                                 + (
                                     :toleransi_menit
                                     * INTERVAL '1 minute'
                                 )
                             )
                        THEN FLOOR(
                            EXTRACT(
                                EPOCH FROM (
                                    :attendance_time
                                    - (
                                        CAST(:attendance_time AS timestamp)::date
                                        + :jam_masuk
                                    )
                                )
                            ) / 60
                        )::integer
                        ELSE 0
                    END
                )
                ON CONFLICT (i_company, i_user, d_absen)
                DO NOTHING
                RETURNING
                    i_absensi,
                    to_char(d_absen, 'YYYY-MM-DD') AS tanggal,
                    to_char(jam_in, 'HH24:MI:SS') AS jam_in,
                    e_status,
                    menit_terlambat,
                    foto_in,
                    alamat_in,
                    e_timezone,
                    i_dinas
                """
            ),
            {
                "i_company": identity["i_company"],
                "i_user": identity["i_user"],
                "i_jadwal": identity["i_jadwal"],
                "i_lokasi": (
                    location["i_lokasi"]
                    if (
                        location is not None
                        and active_dinas is None
                    )
                    else None
                ),
                "i_dinas": (
                    active_dinas["i_dinas"]
                    if active_dinas is not None
                    else None
                ),
                "attendance_time": schedule["waktu_sekarang"],
                "latitude": latitude,
                "longitude": longitude,
                "address": address.strip(),
                "foto_in": relative_photo_path,
                "e_timezone": attendance_timezone,
                "jam_masuk": schedule["jam_masuk"],
                "toleransi_menit": schedule["toleransi_menit"],
            },
        ).mappings().first()

        if attendance is None:
            db.rollback()
            saved_photo_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Anda sudah melakukan absen masuk hari ini.",
            )

        db.commit()
    except HTTPException:
        raise
    except IntegrityError as error:
        db.rollback()
        saved_photo_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Data absensi hari ini sudah tersedia.",
        ) from error
    except Exception:
        db.rollback()
        saved_photo_path.unlink(missing_ok=True)
        raise

    return CheckInResponse(
        status=True,
        message="Absen masuk berhasil.",
        data=CheckInData(
            i_absensi=attendance["i_absensi"],
            tanggal=attendance["tanggal"],
            jam_in=attendance["jam_in"],
            e_status=attendance["e_status"],
            menit_terlambat=attendance["menit_terlambat"],
            distance_meter=(
                round(distance_meter, 2)
                if (
                    distance_meter is not None
                    and active_dinas is None
                )
                else None
            ),
            radius_meter=(
                int(location["radius_meter"])
                if (
                    location is not None
                    and active_dinas is None
                )
                else None
            ),
            e_lokasi_name=(
                f"Dinas LK - {active_dinas['e_kota'] or '-'}"
                if active_dinas is not None
                else (
                    location["e_lokasi_name"]
                    if location is not None
                    else None
                )
            ),
            foto_in=attendance["foto_in"],
            alamat_in=attendance["alamat_in"],
            e_timezone=attendance["e_timezone"],
            is_lk=attendance["i_dinas"] is not None,
            i_dinas=attendance["i_dinas"],
            i_dinas_id=(
                active_dinas["i_dinas_id"]
                if active_dinas is not None
                else None
            ),
            e_kota=(
                active_dinas["e_kota"]
                if active_dinas is not None
                else None
            ),
        ),
    )


@router.post(
    "/check-out",
    response_model=CheckOutResponse,
)
async def check_out(
    latitude: float = Form(..., ge=-90, le=90),
    longitude: float = Form(..., ge=-180, le=180),
    address: str = Form(..., min_length=3, max_length=1000),
    accuracy: float = Form(..., gt=0),
    is_mocked: bool = Form(False),
    photo: UploadFile = File(...),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    validate_gps_security(
        accuracy=accuracy,
        is_mocked=is_mocked,
    )

    if identity["i_jadwal"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Jadwal kerja karyawan belum ditentukan.",
        )

    base_timezone = (
        identity["e_timezone"]
        or "Asia/Jakarta"
    )

    existing_attendance = db.execute(
        text(
            """
            SELECT
                i_absensi,
                d_absen,
                jam_in,
                jam_out,
                i_dinas,
                COALESCE(
                    e_timezone,
                    :base_timezone
                ) AS e_timezone
            FROM public.tm_hr_absensi
            WHERE i_company = :i_company
              AND i_user = :i_user
              AND jam_in IS NOT NULL
              AND jam_out IS NULL
              AND d_absen >= (
                    (
                        CURRENT_TIMESTAMP
                        AT TIME ZONE :base_timezone
                    )::date - 1
              )
            ORDER BY d_absen DESC, jam_in DESC
            LIMIT 1
            """
        ),
        {
            "i_company": identity["i_company"],
            "i_user": identity["i_user"],
            "base_timezone": base_timezone,
        },
    ).mappings().first()

    if existing_attendance is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Anda belum melakukan absen masuk yang dapat dipulangkan.",
        )

    attendance_timezone = (
        existing_attendance["e_timezone"]
        or base_timezone
    )

    dinas_info = None

    if existing_attendance["i_dinas"] is not None:
        dinas_info = db.execute(
            text(
                """
                SELECT
                    d.i_dinas,
                    d.i_dinas_id,

                    CASE
                        WHEN COALESCE(d.f_new, FALSE) = TRUE
                             AND NULLIF(
                                btrim(d.e_kota),
                                ''
                             ) IS NOT NULL
                             AND d.e_kota ~
                                '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                             AND d.e_area ~
                                '^\\s*\\d+(\\s*,\\s*\\d+)*\\s*$'
                        THEN (
                            SELECT string_agg(
                                initcap(c.e_city_name),
                                ', '
                                ORDER BY c.e_city_name
                            )
                            FROM public.tr_city c
                            WHERE c.i_city = ANY (
                                regexp_split_to_array(
                                    btrim(d.e_kota),
                                    '\\s*,\\s*'
                                )::integer[]
                            )
                              AND c.i_area = ANY (
                                regexp_split_to_array(
                                    btrim(d.e_area),
                                    '\\s*,\\s*'
                                )::integer[]
                            )
                        )
                        ELSE d.e_kota
                    END AS e_kota

                FROM public.tm_dinas d
                WHERE d.i_dinas = :i_dinas
                  AND d.i_company = :i_company
                  AND d.i_karyawan = :i_karyawan
                LIMIT 1
                """
            ),
            {
                "i_dinas": existing_attendance["i_dinas"],
                "i_company": identity["i_company"],
                "i_karyawan": identity["i_karyawan"],
            },
        ).mappings().first()

        if dinas_info is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Dokumen dinas pada absensi masuk "
                    "tidak dapat ditemukan. Hubungi HRD."
                ),
            )

    schedule = db.execute(
        text(
            """
            SELECT
                (
                    CURRENT_TIMESTAMP
                    AT TIME ZONE :e_timezone
                ) AS waktu_sekarang,
                h.jam_keluar,
                COALESCE(h.f_libur, FALSE) AS f_libur
            FROM public.tr_hr_jadwal_hari h
            WHERE h.i_jadwal = :i_jadwal
              AND h.i_company = :i_company
              AND h.n_hari = EXTRACT(
                    DOW FROM
                    CAST(:attendance_date AS date)
              )::integer
            LIMIT 1
            """
        ),
        {
            "i_jadwal": identity["i_jadwal"],
            "i_company": identity["i_company"],
            "e_timezone": attendance_timezone,
            "attendance_date": existing_attendance["d_absen"],
        },
    ).mappings().first()

    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal kerja pada tanggal absensi belum tersedia.",
        )

    if schedule["jam_keluar"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Jam pulang hari ini belum dikonfigurasi.",
        )

    location = None
    distance_meter = None

    if identity["i_lokasi_absen"] is not None:
        location = db.execute(
            text(
                """
                SELECT
                    i_lokasi,
                    e_lokasi_name,
                    latitude,
                    longitude,
                    COALESCE(radius_meter, 100) AS radius_meter
                FROM public.tr_hr_lokasi_absen
                WHERE i_lokasi = :i_lokasi
                  AND i_company = :i_company
                  AND f_aktif = TRUE
                LIMIT 1
                """
            ),
            {
                "i_lokasi": identity["i_lokasi_absen"],
                "i_company": identity["i_company"],
            },
        ).mappings().first()

    if (
        dinas_info is None
        and identity["f_wajib_lokasi"] is True
        and location is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lokasi absensi karyawan belum dikonfigurasi.",
        )

    if location is not None:
        distance_meter = calculate_distance_meter(
            latitude,
            longitude,
            float(location["latitude"]),
            float(location["longitude"]),
        )

        if (
            dinas_info is None
            and
            identity["f_wajib_lokasi"] is True
            and distance_meter > int(location["radius_meter"])
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Anda berada di luar radius absensi. "
                    f"Jarak {round(distance_meter)} meter, "
                    f"radius maksimal {location['radius_meter']} meter."
                ),
            )

    if photo.content_type not in {"image/jpeg", "image/jpg"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Foto harus menggunakan format JPG.",
        )

    photo_bytes = await photo.read(MAX_PHOTO_SIZE + 1)
    await photo.close()

    if len(photo_bytes) > MAX_PHOTO_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Ukuran foto maksimal 5 MB.",
        )

    relative_photo_path, saved_photo_path = save_attendance_photo(
        photo_bytes=photo_bytes,
        attendance_type="out",
        i_user=identity["i_user"],
        attendance_time=schedule["waktu_sekarang"],
        employee_name=(
            identity["e_karyawan_name"]
            or identity["e_user_name"]
        ),
        location_name=(
            f"Dinas LK - {dinas_info['e_kota'] or '-'}"
            if dinas_info is not None
            else (
                location["e_lokasi_name"]
                if location is not None
                else "Lokasi fleksibel"
            )
        ),
        address=address.strip(),
        latitude=latitude,
        longitude=longitude,
        attendance_timezone=attendance_timezone,
        dinas_id=(
            dinas_info["i_dinas_id"]
            if dinas_info is not None
            else None
        ),
        dinas_city=(
            dinas_info["e_kota"]
            if dinas_info is not None
            else None
        ),
    )

    try:
        attendance = db.execute(
            text(
                """
                UPDATE public.tm_hr_absensi
                SET
                    jam_out = :attendance_time,
                    metode_out = 'GPS',
                    latitude_out = :latitude,
                    longitude_out = :longitude,
                    alamat_out = :address,
                    foto_out = :foto_out,
                    menit_lembur = CASE
                        WHEN :attendance_time >
                             (
                                 CAST(:attendance_date AS date)
                                 + :jam_keluar
                             )
                        THEN FLOOR(
                            EXTRACT(
                                EPOCH FROM (
                                    :attendance_time
                                    - (
                                        CAST(:attendance_date AS date)
                                        + :jam_keluar
                                    )
                                )
                            ) / 60
                        )::integer
                        ELSE 0
                    END
                WHERE i_absensi = :i_absensi
                  AND i_company = :i_company
                  AND i_user = :i_user
                  AND jam_in IS NOT NULL
                  AND jam_out IS NULL
                RETURNING
                    i_absensi,
                    to_char(d_absen, 'YYYY-MM-DD') AS tanggal,
                    to_char(jam_in, 'HH24:MI:SS') AS jam_in,
                    to_char(jam_out, 'HH24:MI:SS') AS jam_out,
                    e_status,
                    COALESCE(menit_terlambat, 0) AS menit_terlambat,
                    COALESCE(menit_lembur, 0) AS menit_lembur,
                    foto_out,
                    alamat_out,
                    e_timezone,
                    i_dinas
                """
            ),
            {
                "i_absensi": existing_attendance["i_absensi"],
                "i_company": identity["i_company"],
                "i_user": identity["i_user"],
                "attendance_time": schedule["waktu_sekarang"],
                "attendance_date": existing_attendance["d_absen"],
                "latitude": latitude,
                "longitude": longitude,
                "address": address.strip(),
                "foto_out": relative_photo_path,
                "jam_keluar": schedule["jam_keluar"],
            },
        ).mappings().first()

        if attendance is None:
            db.rollback()
            saved_photo_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Absen pulang sudah diproses atau data berubah.",
            )

        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        saved_photo_path.unlink(missing_ok=True)
        raise

    return CheckOutResponse(
        status=True,
        message="Absen pulang berhasil.",
        data=CheckOutData(
            i_absensi=attendance["i_absensi"],
            tanggal=attendance["tanggal"],
            jam_in=attendance["jam_in"],
            jam_out=attendance["jam_out"],
            e_status=attendance["e_status"],
            menit_terlambat=attendance["menit_terlambat"],
            menit_lembur=attendance["menit_lembur"],
            distance_meter=(
                round(distance_meter, 2)
                if (
                    distance_meter is not None
                    and dinas_info is None
                )
                else None
            ),
            radius_meter=(
                int(location["radius_meter"])
                if (
                    location is not None
                    and dinas_info is None
                )
                else None
            ),
            e_lokasi_name=(
                f"Dinas LK - {dinas_info['e_kota'] or '-'}"
                if dinas_info is not None
                else (
                    location["e_lokasi_name"]
                    if location is not None
                    else None
                )
            ),
            foto_out=attendance["foto_out"],
            alamat_out=attendance["alamat_out"],
            e_timezone=attendance["e_timezone"],
            is_lk=attendance["i_dinas"] is not None,
            i_dinas=attendance["i_dinas"],
            i_dinas_id=(
                dinas_info["i_dinas_id"]
                if dinas_info is not None
                else None
            ),
            e_kota=(
                dinas_info["e_kota"]
                if dinas_info is not None
                else None
            ),
        ),
    )
