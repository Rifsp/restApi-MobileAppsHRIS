from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_identity
from app.schemas.announcements import (
    AnnouncementDetailData,
    AnnouncementDetailResponse,
    AnnouncementItem,
    AnnouncementListData,
    AnnouncementListResponse,
    AnnouncementReadData,
    AnnouncementReadResponse,
    AnnouncementUnreadData,
    AnnouncementUnreadResponse,
)


router = APIRouter(prefix="/api/announcements", tags=["Announcements"])


VISIBLE_ANNOUNCEMENT_WHERE = """
    p.i_company = :i_company
    AND p.e_status = 'PUBLISHED'
    AND p.f_aktif = TRUE
    AND p.d_mulai <= (
        CURRENT_TIMESTAMP AT TIME ZONE :e_timezone
    )::date
    AND (
        p.d_selesai IS NULL
        OR p.d_selesai >= (
            CURRENT_TIMESTAMP AT TIME ZONE :e_timezone
        )::date
    )
    AND EXISTS (
        SELECT 1
        FROM public.tm_hr_pengumuman_target t
        WHERE t.i_pengumuman = p.i_pengumuman
          AND (
              t.e_target_type = 'ALL'
              OR (
                  t.e_target_type = 'USER'
                  AND t.i_user = :i_user
              )
              OR (
                  t.e_target_type = 'STORE'
                  AND t.i_store = :i_store
              )
              OR (
                  t.e_target_type = 'DEPARTMENT'
                  AND t.i_department = :i_department
              )
          )
    )
"""


def announcement_context(db: Session, identity) -> dict:
    employee = db.execute(
        text(
            """
            SELECT i_store, i_department
            FROM public.tr_hr_karyawan
            WHERE i_karyawan = :i_karyawan
              AND i_user = :i_user
              AND i_company = :i_company
              AND f_aktif = TRUE
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
            detail="Data store atau department karyawan belum tersedia.",
        )

    return {
        "i_user": identity["i_user"],
        "i_company": identity["i_company"],
        "i_store": employee["i_store"],
        "i_department": employee["i_department"],
        "e_timezone": identity["e_timezone"] or "Asia/Jakarta",
    }


def to_item(row) -> AnnouncementItem:
    return AnnouncementItem(
        i_pengumuman=row["i_pengumuman"],
        e_judul=row["e_judul"],
        e_isi=row["e_isi"],
        d_mulai=row["d_mulai"],
        d_selesai=row["d_selesai"],
        d_publish=row["d_publish"],
        is_read=row["d_baca"] is not None,
        d_baca=row["d_baca"],
    )


@router.get("", response_model=AnnouncementListResponse)
def list_announcements(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=50),
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    params = announcement_context(db, identity)
    params.update({"limit": limit, "offset": (page - 1) * limit})

    rows = db.execute(
        text(
            f"""
            SELECT
                p.i_pengumuman,
                p.e_judul,
                p.e_isi,
                p.d_mulai,
                p.d_selesai,
                p.d_publish,
                b.d_baca
            FROM public.tm_hr_pengumuman p
            LEFT JOIN public.tm_hr_pengumuman_baca b
              ON b.i_pengumuman = p.i_pengumuman
             AND b.i_company = :i_company
             AND b.i_user = :i_user
            WHERE {VISIBLE_ANNOUNCEMENT_WHERE}
            ORDER BY p.d_publish DESC NULLS LAST, p.i_pengumuman DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    counts = db.execute(
        text(
            f"""
            SELECT
                COUNT(*)::integer AS total,
                COUNT(*) FILTER (WHERE b.i_baca IS NULL)::integer AS unread
            FROM public.tm_hr_pengumuman p
            LEFT JOIN public.tm_hr_pengumuman_baca b
              ON b.i_pengumuman = p.i_pengumuman
             AND b.i_company = :i_company
             AND b.i_user = :i_user
            WHERE {VISIBLE_ANNOUNCEMENT_WHERE}
            """
        ),
        params,
    ).mappings().one()

    return AnnouncementListResponse(
        status=True,
        message="Daftar pengumuman berhasil dimuat.",
        data=AnnouncementListData(
            items=[to_item(row) for row in rows],
            page=page,
            limit=limit,
            total=counts["total"],
            unread=counts["unread"],
        ),
    )


@router.get("/unread-count", response_model=AnnouncementUnreadResponse)
def unread_count(
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    params = announcement_context(db, identity)
    unread = db.execute(
        text(
            f"""
            SELECT COUNT(*)::integer
            FROM public.tm_hr_pengumuman p
            LEFT JOIN public.tm_hr_pengumuman_baca b
              ON b.i_pengumuman = p.i_pengumuman
             AND b.i_company = :i_company
             AND b.i_user = :i_user
            WHERE {VISIBLE_ANNOUNCEMENT_WHERE}
              AND b.i_baca IS NULL
            """
        ),
        params,
    ).scalar_one()

    return AnnouncementUnreadResponse(
        status=True,
        message="Jumlah pengumuman belum dibaca berhasil dimuat.",
        data=AnnouncementUnreadData(unread=unread),
    )


@router.get("/{announcement_id}", response_model=AnnouncementDetailResponse)
def announcement_detail(
    announcement_id: int,
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    params = announcement_context(db, identity)
    params["announcement_id"] = announcement_id
    row = db.execute(
        text(
            f"""
            SELECT
                p.i_pengumuman,
                p.e_judul,
                p.e_isi,
                p.d_mulai,
                p.d_selesai,
                p.d_publish,
                b.d_baca
            FROM public.tm_hr_pengumuman p
            LEFT JOIN public.tm_hr_pengumuman_baca b
              ON b.i_pengumuman = p.i_pengumuman
             AND b.i_company = :i_company
             AND b.i_user = :i_user
            WHERE p.i_pengumuman = :announcement_id
              AND {VISIBLE_ANNOUNCEMENT_WHERE}
            LIMIT 1
            """
        ),
        params,
    ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengumuman tidak ditemukan atau bukan untuk pengguna ini.",
        )

    return AnnouncementDetailResponse(
        status=True,
        message="Detail pengumuman berhasil dimuat.",
        data=AnnouncementDetailData(announcement=to_item(row)),
    )


@router.post("/{announcement_id}/read", response_model=AnnouncementReadResponse)
def mark_announcement_read(
    announcement_id: int,
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    params = announcement_context(db, identity)
    params["announcement_id"] = announcement_id
    visible = db.execute(
        text(
            f"""
            SELECT p.i_pengumuman
            FROM public.tm_hr_pengumuman p
            WHERE p.i_pengumuman = :announcement_id
              AND {VISIBLE_ANNOUNCEMENT_WHERE}
            LIMIT 1
            """
        ),
        params,
    ).first()
    if visible is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengumuman tidak ditemukan atau bukan untuk pengguna ini.",
        )

    read_row = db.execute(
        text(
            """
            INSERT INTO public.tm_hr_pengumuman_baca (
                i_pengumuman, i_company, i_user, d_baca
            ) VALUES (
                :announcement_id, :i_company, :i_user, now()
            )
            ON CONFLICT (i_pengumuman, i_user)
            DO UPDATE SET i_company = EXCLUDED.i_company
            RETURNING d_baca
            """
        ),
        params,
    ).mappings().one()
    db.commit()

    return AnnouncementReadResponse(
        status=True,
        message="Pengumuman ditandai sudah dibaca.",
        data=AnnouncementReadData(
            i_pengumuman=announcement_id,
            is_read=True,
            d_baca=read_row["d_baca"],
        ),
    )
