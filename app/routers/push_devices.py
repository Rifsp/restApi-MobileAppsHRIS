from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_identity
from app.schemas.push_devices import (
    PushDeviceData,
    PushDeviceRegisterRequest,
    PushDeviceResponse,
    PushDeviceUnregisterData,
    PushDeviceUnregisterRequest,
    PushDeviceUnregisterResponse,
)


router = APIRouter(prefix="/api/push-devices", tags=["Push Devices"])


@router.post("/register", response_model=PushDeviceResponse)
def register_push_device(
    request: PushDeviceRegisterRequest,
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    try:
        # Token push dapat berpindah ketika aplikasi dipasang ulang atau user
        # berganti akun. Baris lama dibuang agar unique token tetap konsisten.
        db.execute(
            text(
                """
                DELETE FROM public.tm_hr_push_device
                WHERE e_push_token = :push_token
                  AND f_aktif = FALSE
                  AND NOT (
                      i_user = :i_user
                      AND i_company = :i_company
                      AND e_device_id = :device_id
                  )
                """
            ),
            {
                "push_token": request.push_token,
                "i_user": identity["i_user"],
                "i_company": identity["i_company"],
                "device_id": request.device_id,
            },
        )

        device = db.execute(
            text(
                """
                INSERT INTO public.tm_hr_push_device (
                    i_user,
                    i_company,
                    e_device_id,
                    e_push_token,
                    e_platform,
                    e_app_version,
                    f_aktif,
                    d_register,
                    d_update
                ) VALUES (
                    :i_user,
                    :i_company,
                    :device_id,
                    :push_token,
                    :platform,
                    :app_version,
                    TRUE,
                    now(),
                    NULL
                )
                ON CONFLICT (i_user, i_company, e_device_id)
                DO UPDATE SET
                    e_push_token = EXCLUDED.e_push_token,
                    e_platform = EXCLUDED.e_platform,
                    e_app_version = EXCLUDED.e_app_version,
                    f_aktif = TRUE,
                    d_update = now()
                RETURNING
                    i_push_device,
                    e_device_id,
                    e_platform,
                    e_app_version,
                    f_aktif,
                    d_register,
                    d_update
                """
            ),
            {
                "i_user": identity["i_user"],
                "i_company": identity["i_company"],
                "device_id": request.device_id,
                "push_token": request.push_token,
                "platform": request.platform,
                "app_version": request.app_version,
            },
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Token perangkat sedang digunakan oleh sesi lain.",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Registrasi perangkat gagal. Coba kembali.",
        ) from exc

    return PushDeviceResponse(
        status=True,
        message="Perangkat berhasil didaftarkan.",
        data=PushDeviceData(
            i_push_device=device["i_push_device"],
            device_id=device["e_device_id"],
            platform=device["e_platform"],
            app_version=device["e_app_version"],
            active=device["f_aktif"] is True,
            registered_at=device["d_register"],
            updated_at=device["d_update"],
        ),
    )


@router.post("/unregister", response_model=PushDeviceUnregisterResponse)
def unregister_push_device(
    request: PushDeviceUnregisterRequest,
    identity=Depends(get_current_identity),
    db: Session = Depends(get_db),
):
    db.execute(
        text(
            """
            UPDATE public.tm_hr_push_device
            SET f_aktif = FALSE,
                d_update = now()
            WHERE i_user = :i_user
              AND i_company = :i_company
              AND e_device_id = :device_id
              AND f_aktif = TRUE
            """
        ),
        {
            "i_user": identity["i_user"],
            "i_company": identity["i_company"],
            "device_id": request.device_id,
        },
    )
    db.commit()

    return PushDeviceUnregisterResponse(
        status=True,
        message="Perangkat berhasil dinonaktifkan.",
        data=PushDeviceUnregisterData(
            device_id=request.device_id,
            active=False,
        ),
    )
