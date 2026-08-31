from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas.auth import (
    CurrentUserData,
    CurrentUserResponse,
    LoginData,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RefreshData,
    RefreshRequest,
    RefreshResponse,
    UserResponse,
)
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_token,
    verify_legacy_password,
)


bearer_scheme = HTTPBearer(auto_error=False)

router = APIRouter(
    prefix="/api/auth",
    tags=["Authentication"],
)


def get_active_employee(
    db: Session,
    user_id: int,
):
    employees = db.execute(
        text(
            """
            SELECT
                k.i_karyawan,
                k.i_user,
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
            FROM public.tr_hr_karyawan k
            INNER JOIN public.tr_company c
                ON c.i_company = k.i_company
            WHERE k.i_user = :user_id
              AND k.f_aktif = TRUE
            ORDER BY k.i_company
            """
        ),
        {
            "user_id": user_id,
        },
    ).mappings().all()

    if len(employees) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Akun belum terdaftar sebagai "
                "karyawan aktif."
            ),
        )

    if len(employees) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Akun memiliki lebih dari satu data "
                "karyawan aktif. Hubungi MIS."
            ),
        )

    return employees[0]


def make_user_response(
    user,
    employee,
) -> UserResponse:
    return UserResponse(
        i_user=user["i_user"],
        i_karyawan=employee["i_karyawan"],
        i_user_id=user["i_user_id"],
        e_user_name=user["e_user_name"],
        e_karyawan_name=(
            employee["e_karyawan_name"]
            or user["e_user_name"]
        ),
        f_pusat=user["f_pusat"],
        ava=user["ava"],
        i_company=employee["i_company"],
        e_company_name=employee["e_company_name"],
        e_nik=employee["e_nik"],
        e_tipe_karyawan=employee["e_tipe_karyawan"],
        f_wajib_lokasi=(
            employee["f_wajib_lokasi"] is True
        ),
        i_jadwal=employee["i_jadwal"],
        i_lokasi_absen=employee["i_lokasi_absen"],
        e_timezone=employee["e_timezone"],
    )


@router.post(
    "/login",
    response_model=LoginResponse,
)
def login(
    request: LoginRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    username = request.username.strip()
    password = request.password.strip()

    invalid_login = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Username atau password salah.",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )

    user = db.execute(
        text(
            """
            SELECT
                i_user,
                i_user_id,
                e_user_password,
                e_user_name,
                f_status,
                f_pusat,
                ava
            FROM public.tm_user
            WHERE i_user_id = :username
            LIMIT 1
            """
        ),
        {
            "username": username,
        },
    ).mappings().first()

    if user is None:
        raise invalid_login

    if user["f_status"] is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akun sudah tidak aktif.",
        )

    if not verify_legacy_password(
        password,
        user["e_user_password"],
    ):
        raise invalid_login

    employee = get_active_employee(
        db=db,
        user_id=user["i_user"],
    )

    refresh_token, refresh_expires_in = create_refresh_token()
    now = datetime.now(timezone.utc)

    # Satu perangkat hanya memiliki satu sesi aktif untuk user/company
    # yang sama. Login baru pada device yang sama mencabut sesi lama.
    if request.device_id:
        db.execute(
            text(
                """
                UPDATE public.tm_hr_session
                SET f_active = FALSE,
                    d_logout = now()
                WHERE i_user = :user_id
                  AND i_company = :company_id
                  AND e_device_id = :device_id
                  AND f_active = TRUE
                """
            ),
            {
                "user_id": user["i_user"],
                "company_id": employee["i_company"],
                "device_id": request.device_id,
            },
        )

    new_session = db.execute(
        text(
            """
            INSERT INTO public.tm_hr_session (
                i_user, i_company,
                access_token_hash, refresh_token_hash,
                d_access_expired, d_refresh_expired,
                d_last_activity,
                e_device_id, e_device_name,
                e_platform, e_app_version,
                ip_address, f_active
            ) VALUES (
                :user_id, :company_id,
                :access_hash, :refresh_hash,
                :access_expired, :refresh_expired,
                :last_activity,
                :device_id, :device_name,
                :platform, :app_version,
                :ip_address, TRUE
            )
            RETURNING i_session
            """
        ),
        {
            "user_id": user["i_user"],
            "company_id": employee["i_company"],
            # Nilai sementara yang unik; diganti setelah i_session didapat.
            "access_hash": hash_token(refresh_token + ":pending"),
            "refresh_hash": hash_token(refresh_token),
            "access_expired": now,
            "refresh_expired": now + timedelta(
                seconds=refresh_expires_in
            ),
            "last_activity": now,
            "device_id": request.device_id,
            "device_name": request.device_name,
            "platform": request.platform,
            "app_version": request.app_version,
            "ip_address": (
                http_request.client.host
                if http_request.client is not None
                else None
            ),
        },
    ).mappings().one()

    session_id = int(new_session["i_session"])
    access_token, expires_in = create_access_token(
        subject=str(user["i_user"]),
        additional_claims={
            "sid": session_id,
            "username": user["i_user_id"],
            "name": user["e_user_name"],
            "i_company": employee["i_company"],
        },
    )

    db.execute(
        text(
            """
            UPDATE public.tm_hr_session
            SET access_token_hash = :access_hash,
                d_access_expired = :access_expired
            WHERE i_session = :session_id
            """
        ),
        {
            "access_hash": hash_token(access_token),
            "access_expired": now + timedelta(seconds=expires_in),
            "session_id": session_id,
        },
    )
    db.commit()

    return LoginResponse(
        status=True,
        message="Login berhasil.",
        data=LoginData(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            refresh_expires_in=refresh_expires_in,
            user=make_user_response(
                user=user,
                employee=employee,
            ),
        ),
    )


@router.post("/refresh", response_model=RefreshResponse)
def refresh_access_token(
    request: RefreshRequest,
    db: Session = Depends(get_db),
):
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Refresh token tidak valid atau sudah berakhir.",
    )
    supplied_hash = hash_token(request.refresh_token)
    old_session = db.execute(
        text(
            """
            SELECT s.*
            FROM public.tm_hr_session s
            INNER JOIN public.tm_user u
                ON u.i_user = s.i_user
               AND u.f_status = TRUE
            WHERE s.refresh_token_hash = :token_hash
              AND s.f_active = TRUE
              AND s.d_refresh_expired > now()
            FOR UPDATE
            """
        ),
        {"token_hash": supplied_hash},
    ).mappings().first()

    if old_session is None:
        # Jika token sebelumnya dipakai kembali setelah rotasi, sesi aktif
        # dianggap berisiko dan langsung dicabut.
        reused_session = db.execute(
            text(
                """
                SELECT i_session
                FROM public.tm_hr_session
                WHERE previous_refresh_token_hash = :token_hash
                  AND f_active = TRUE
                FOR UPDATE
                """
            ),
            {"token_hash": supplied_hash},
        ).mappings().first()

        if reused_session is not None:
            db.execute(
                text(
                    """
                    UPDATE public.tm_hr_session
                    SET f_active = FALSE,
                        d_logout = now()
                    WHERE i_session = :session_id
                    """
                ),
                {"session_id": reused_session["i_session"]},
            )
            db.commit()
        else:
            db.rollback()
        raise unauthorized

    employee = get_active_employee(db, old_session["i_user"])
    if employee["i_company"] != old_session["i_company"]:
        db.rollback()
        raise unauthorized

    now = datetime.now(timezone.utc)
    refresh_expires_in = max(
        0,
        int(
            (old_session["d_refresh_expired"] - now).total_seconds()
        ),
    )
    if refresh_expires_in <= 0:
        db.rollback()
        raise unauthorized

    access_lifetime = min(
        settings.jwt_expire_minutes * 60,
        refresh_expires_in,
    )
    access_token, expires_in = create_access_token(
        subject=str(old_session["i_user"]),
        additional_claims={
            "sid": old_session["i_session"],
            "i_company": old_session["i_company"],
        },
        expires_in_seconds=access_lifetime,
    )
    refresh_token, _ = create_refresh_token()

    db.execute(
        text(
            """
            UPDATE public.tm_hr_session
            SET previous_refresh_token_hash = refresh_token_hash,
                refresh_token_hash = :refresh_hash,
                access_token_hash = :access_hash,
                d_access_expired = :access_expired,
                d_last_activity = :last_activity
            WHERE i_session = :session_id
              AND f_active = TRUE
            """
        ),
        {
            "session_id": old_session["i_session"],
            "access_hash": hash_token(access_token),
            "refresh_hash": hash_token(refresh_token),
            "access_expired": now + timedelta(seconds=expires_in),
            "last_activity": now,
        },
    )
    db.commit()

    return RefreshResponse(
        status=True,
        message="Token berhasil diperbarui.",
        data=RefreshData(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            refresh_expires_in=refresh_expires_in,
        ),
    )


@router.post("/logout", response_model=LogoutResponse)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
    db: Session = Depends(get_db),
):
    if credentials is not None:
        db.execute(
            text(
                """
                WITH ended_session AS (
                    UPDATE public.tm_hr_session
                    SET f_active = FALSE,
                        d_logout = now()
                    WHERE access_token_hash = :token_hash
                      AND f_active = TRUE
                    RETURNING i_user, i_company, e_device_id
                )
                UPDATE public.tm_hr_push_device p
                SET f_aktif = FALSE,
                    d_update = now()
                FROM ended_session s
                WHERE s.e_device_id IS NOT NULL
                  AND p.i_user = s.i_user
                  AND p.i_company = s.i_company
                  AND p.e_device_id = s.e_device_id
                  AND p.f_aktif = TRUE
                """
            ),
            {"token_hash": hash_token(credentials.credentials)},
        )
        db.commit()

    return LogoutResponse(status=True, message="Logout berhasil.")


@router.get(
    "/me",
    response_model=CurrentUserResponse,
)
def get_current_user(
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

    user = db.execute(
        text(
            """
            SELECT
                i_user,
                i_user_id,
                e_user_name,
                f_status,
                f_pusat,
                ava
            FROM public.tm_user
            WHERE i_user = :user_id
              AND f_status = TRUE
            LIMIT 1
            """
        ),
        {
            "user_id": user_id,
        },
    ).mappings().first()

    if user is None:
        raise unauthorized

    employee = get_active_employee(
        db=db,
        user_id=user_id,
    )

    # Jika perusahaan karyawan berubah, token lama
    # harus dibuang dan pengguna login kembali. 
    if employee["i_company"] != token_company:
        raise unauthorized

    return CurrentUserResponse(
        status=True,
        message="Sesi pengguna valid.",
        data=CurrentUserData(
            user=make_user_response(
                user=user,
                employee=employee,
            )
        ),
    )
