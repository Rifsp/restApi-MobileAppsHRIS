from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.security import decode_access_token, hash_token


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
    db: Session = Depends(get_db),
):
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sesi tidak valid atau sudah berakhir.",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )

    if credentials is None:
        raise unauthorized

    if credentials.scheme.lower() != "bearer":
        raise unauthorized

    payload = decode_access_token(
        credentials.credentials
    )

    if payload is None:
        raise unauthorized

    try:
        user_id = int(payload["sub"])
        token_company = int(payload["i_company"])
        session_id = int(payload["sid"])
    except (KeyError, TypeError, ValueError):
        raise unauthorized

    active_session = db.execute(
        text(
            """
            SELECT i_session
            FROM public.tm_hr_session
            WHERE i_session = :session_id
              AND access_token_hash = :token_hash
              AND i_user = :user_id
              AND i_company = :company_id
              AND f_active = TRUE
              AND d_access_expired > now()
            LIMIT 1
            """
        ),
        {
            "token_hash": hash_token(credentials.credentials),
            "session_id": session_id,
            "user_id": user_id,
            "company_id": token_company,
        },
    ).first()

    if active_session is None:
        raise unauthorized

    activity_updated = db.execute(
        text(
            """
            UPDATE public.tm_hr_session
            SET d_last_activity = now()
            WHERE i_session = :session_id
              AND f_active = TRUE
              AND (
                  d_last_activity IS NULL
                  OR d_last_activity < now() - interval '5 minutes'
              )
            RETURNING i_session
            """
        ),
        {"session_id": session_id},
    ).first()
    if activity_updated is not None:
        db.commit()

    identities = db.execute(
        text(
            """
            SELECT
                u.i_user,
                u.i_user_id,
                u.e_user_name,
                u.f_status,
                u.f_pusat,
                u.ava,

                k.i_karyawan,
                k.i_company,
                k.e_nik,
                k.e_karyawan_name,
                k.e_tipe_karyawan,
                k.f_wajib_lokasi,
                k.i_jadwal,
                k.i_lokasi_absen,
                k.f_aktif,

                COALESCE(
                    k.e_timezone,
                    'Asia/Jakarta'
                ) AS e_timezone,

                c.e_company_name

            FROM public.tm_user u

            INNER JOIN public.tr_hr_karyawan k
                ON k.i_user = u.i_user
                AND k.f_aktif = TRUE

            INNER JOIN public.tr_company c
                ON c.i_company = k.i_company

            WHERE u.i_user = :user_id
              AND u.f_status = TRUE
            """
        ),
        {
            "user_id": user_id,
        },
    ).mappings().all()

    if len(identities) == 0:
        raise unauthorized

    if len(identities) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Akun memiliki lebih dari satu data "
                "karyawan aktif."
            ),
        )

    identity = identities[0]

    if identity["i_company"] != token_company:
        raise unauthorized

    return identity
