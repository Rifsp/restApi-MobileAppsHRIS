from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_identity
from app.schemas.dashboard import (
    DashboardAttendance,
    DashboardData,
    DashboardDinas,
    DashboardHoliday,
    DashboardLocation,
    DashboardProfile,
    DashboardResponse,
    DashboardSchedule,
)


router = APIRouter(
    prefix="/api",
    tags=["Dashboard"],
)


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
)
def get_dashboard(
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    if identity["i_jadwal"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Jadwal kerja karyawan belum ditentukan.",
        )

    employee_timezone = (
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
            "e_timezone": employee_timezone,
        },
    ).scalar_one()

    active_dinas_rows = db.execute(
        text(
            """
            SELECT
                d.i_dinas,
                d.i_dinas_id,

                CASE
                    WHEN COALESCE(d.f_new, FALSE) = TRUE
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

                CASE
                    WHEN COALESCE(d.f_new, FALSE) = TRUE
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

                d.e_remark,
                d.d_berangkat,
                d.d_kembali
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
            "i_karyawan": identity["i_karyawan"],
            "i_company": identity["i_company"],
            "attendance_date": base_attendance_date,
        },
    ).mappings().all()

    if len(active_dinas_rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Ditemukan lebih dari satu penugasan "
                "dinas aktif pada tanggal ini. Hubungi HRD."
            ),
        )

    active_dinas = (
        active_dinas_rows[0]
        if active_dinas_rows
        else None
    )

    holiday = db.execute(
        text(
            """
            SELECT
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
            "i_company": identity["i_company"],
            "attendance_date": base_attendance_date,
        },
    ).mappings().first()

    attendance = db.execute(
        text(
            """
            SELECT
                a.i_absensi,

                to_char(
                    a.jam_in,
                    'HH24:MI'
                ) AS jam_in,

                to_char(
                    a.jam_out,
                    'HH24:MI'
                ) AS jam_out,

                a.metode_in,
                a.metode_out,

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

                COALESCE(
                    a.e_timezone,
                    :employee_timezone
                ) AS e_timezone,

                (a.i_dinas IS NOT NULL) AS is_lk,
                a.i_dinas,
                d.i_dinas_id,

                CASE
                    WHEN COALESCE(d.f_new, FALSE) = TRUE
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
            LEFT JOIN public.tm_dinas d
                ON d.i_dinas = a.i_dinas
                AND d.i_company = a.i_company

            WHERE a.i_user = :i_user
              AND a.i_company = :i_company
              AND a.d_absen = (
                    CURRENT_TIMESTAMP
                    AT TIME ZONE COALESCE(
                        a.e_timezone,
                        :employee_timezone
                    )
              )::date

            ORDER BY a.d_absen DESC
            LIMIT 1
            """
        ),
        {
            "i_user": identity["i_user"],
            "i_company": identity["i_company"],
            "employee_timezone": employee_timezone,
        },
    ).mappings().first()

    dashboard_timezone = (
        attendance["e_timezone"]
        if attendance is not None
        else employee_timezone
    )

    schedule = db.execute(
        text(
            """
            SELECT
                (
                    CURRENT_TIMESTAMP
                    AT TIME ZONE :e_timezone
                )::date AS tanggal,

                h.n_hari,

                CASE
                    WHEN h.f_libur = TRUE THEN NULL
                    ELSE to_char(
                        h.jam_masuk,
                        'HH24:MI'
                    )
                END AS jam_masuk,

                CASE
                    WHEN h.f_libur = TRUE THEN NULL
                    ELSE to_char(
                        h.jam_keluar,
                        'HH24:MI'
                    )
                END AS jam_keluar,

                COALESCE(
                    h.toleransi_menit,
                    0
                ) AS toleransi_menit,

                COALESCE(
                    h.f_libur,
                    FALSE
                ) AS f_libur

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
            "e_timezone": dashboard_timezone,
        },
    ).mappings().first()

    if schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jadwal kerja hari ini belum tersedia.",
        )

    location = None

    if identity["i_lokasi_absen"] is not None:
        location = db.execute(
            text(
                """
                SELECT
                    i_lokasi,
                    e_lokasi_name,
                    latitude,
                    longitude,
                    COALESCE(
                        radius_meter,
                        100
                    ) AS radius_meter
                FROM public.tr_hr_lokasi_absen
                WHERE i_lokasi = :i_lokasi
                  AND i_company = :i_company
                  AND f_aktif = TRUE
                LIMIT 1
                """
            ),
            {
                "i_lokasi": identity[
                    "i_lokasi_absen"
                ],
                "i_company": identity["i_company"],
            },
        ).mappings().first()

    if (
        identity["f_wajib_lokasi"] is True
        and location is None
        and active_dinas is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Lokasi absensi karyawan "
                "belum dikonfigurasi."
            ),
        )

    is_holiday = (
        schedule["f_libur"] is True
        or holiday is not None
    )

    can_check_in_regular = (
        not is_holiday
        or identity["f_wajib_lokasi"] is False
    )
    can_check_in_lk = active_dinas is not None
    can_check_in = (
        can_check_in_regular
        or can_check_in_lk
    )

    if attendance is None:
        attendance_data = DashboardAttendance(
            i_absensi=None,
            jam_in=None,
            jam_out=None,
            metode_in=None,
            metode_out=None,
            e_status=(
                "LIBUR"
                if is_holiday and not can_check_in
                else "BELUM_ABSEN"
            ),
            menit_terlambat=0,
            menit_lembur=0,
            is_lk=False,
            i_dinas=None,
            i_dinas_id=None,
            e_kota=None,
            can_check_in=can_check_in,
            can_check_in_regular=can_check_in_regular,
            can_check_in_lk=can_check_in_lk,
            can_check_out=False,
        )
    else:
        has_checked_in = (
            attendance["jam_in"] is not None
        )

        has_checked_out = (
            attendance["jam_out"] is not None
        )

        attendance_data = DashboardAttendance(
            i_absensi=attendance["i_absensi"],
            jam_in=attendance["jam_in"],
            jam_out=attendance["jam_out"],
            metode_in=attendance["metode_in"],
            metode_out=attendance["metode_out"],
            e_status=attendance["e_status"],
            menit_terlambat=attendance[
                "menit_terlambat"
            ],
            menit_lembur=attendance[
                "menit_lembur"
            ],
            is_lk=attendance["is_lk"],
            i_dinas=attendance["i_dinas"],
            i_dinas_id=attendance[
                "i_dinas_id"
            ],
            e_kota=attendance["e_kota"],
            can_check_in=False,
            can_check_in_regular=False,
            can_check_in_lk=False,
            can_check_out=(
                has_checked_in
                and not has_checked_out
            ),
        )

    location_data = None

    if location is not None:
        location_data = DashboardLocation(
            i_lokasi=location["i_lokasi"],
            e_lokasi_name=location[
                "e_lokasi_name"
            ],
            latitude=float(location["latitude"]),
            longitude=float(location["longitude"]),
            radius_meter=location[
                "radius_meter"
            ],
        )

    return DashboardResponse(
        status=True,
        message="Dashboard berhasil dimuat.",
        data=DashboardData(
            profile=DashboardProfile(
                i_user=identity["i_user"],
                i_karyawan=identity["i_karyawan"],
                i_user_id=identity["i_user_id"],
                e_user_name=identity[
                    "e_user_name"
                ],
                e_karyawan_name=(
                    identity["e_karyawan_name"]
                    or identity["e_user_name"]
                ),
                ava=identity["ava"],
                i_company=identity["i_company"],
                e_company_name=identity[
                    "e_company_name"
                ],
                e_nik=identity["e_nik"],
                e_tipe_karyawan=identity[
                    "e_tipe_karyawan"
                ],
                f_wajib_lokasi=(
                    identity[
                        "f_wajib_lokasi"
                    ] is True
                ),
                e_timezone=dashboard_timezone,
            ),
            schedule=DashboardSchedule(
                tanggal=schedule["tanggal"],
                n_hari=schedule["n_hari"],
                jam_masuk=schedule["jam_masuk"],
                jam_keluar=schedule["jam_keluar"],
                toleransi_menit=schedule[
                    "toleransi_menit"
                ],
                f_libur=schedule["f_libur"],
                e_timezone=dashboard_timezone,
            ),
            location=location_data,
            active_dinas=(
                DashboardDinas(**active_dinas)
                if active_dinas is not None
                else None
            ),
            holiday=(
                DashboardHoliday(**holiday)
                if holiday is not None
                else None
            ),
            attendance_today=attendance_data,
        ),
    )
