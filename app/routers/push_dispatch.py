import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db


router = APIRouter(prefix="/api/internal/push", tags=["Internal Push"])
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def require_internal_key(x_internal_key: str | None = Header(default=None)):
    expected = settings.internal_api_key
    if (
        not expected
        or not x_internal_key
        or not hmac.compare_digest(x_internal_key, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal API key tidak valid.",
        )


def send_expo_batch(messages: list[dict]) -> list[dict]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if settings.expo_push_access_token:
        headers["Authorization"] = (
            f"Bearer {settings.expo_push_access_token}"
        )

    request = Request(
        EXPO_PUSH_URL,
        data=json.dumps(messages).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Layanan push Expo belum dapat dihubungi.",
        ) from exc

    tickets = payload.get("data")
    if not isinstance(tickets, list) or len(tickets) != len(messages):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Respons layanan push Expo tidak valid.",
        )
    return tickets


@router.post("/announcements/{announcement_id}/dispatch")
def dispatch_announcement(
    announcement_id: int,
    _=Depends(require_internal_key),
    db: Session = Depends(get_db),
):
    announcement = db.execute(
        text(
            """
            SELECT i_pengumuman, i_company, e_judul, e_isi
            FROM public.tm_hr_pengumuman
            WHERE i_pengumuman = :announcement_id
              AND e_status = 'PUBLISHED'
              AND f_aktif = TRUE
              AND f_push_notification = TRUE
            LIMIT 1
            """
        ),
        {"announcement_id": announcement_id},
    ).mappings().first()
    if announcement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengumuman aktif untuk push tidak ditemukan.",
        )

    devices = db.execute(
        text(
            """
            SELECT DISTINCT
                pd.i_push_device,
                pd.e_push_token
            FROM public.tm_hr_push_device pd
            INNER JOIN public.tr_hr_karyawan k
              ON k.i_user = pd.i_user
             AND k.i_company = pd.i_company
             AND k.f_aktif = TRUE
            WHERE pd.i_company = :i_company
              AND pd.f_aktif = TRUE
              AND EXISTS (
                  SELECT 1
                  FROM public.tm_hr_pengumuman_target t
                  WHERE t.i_pengumuman = :announcement_id
                    AND (
                        t.e_target_type = 'ALL'
                        OR (t.e_target_type = 'USER' AND t.i_user = pd.i_user)
                        OR (t.e_target_type = 'STORE' AND t.i_store = k.i_store)
                        OR (
                            t.e_target_type = 'DEPARTMENT'
                            AND t.i_department = k.i_department
                        )
                    )
              )
            ORDER BY pd.i_push_device
            """
        ),
        {
            "announcement_id": announcement_id,
            "i_company": announcement["i_company"],
        },
    ).mappings().all()

    queued = []
    for device in devices:
        row = db.execute(
            text(
                """
                INSERT INTO public.tm_hr_push_delivery (
                    i_pengumuman, i_push_device, e_status
                ) VALUES (
                    :announcement_id, :i_push_device, 'QUEUED'
                )
                ON CONFLICT (i_pengumuman, i_push_device)
                DO UPDATE SET
                    e_status = 'QUEUED',
                    e_ticket_id = NULL,
                    e_error = NULL,
                    d_update = now()
                WHERE public.tm_hr_push_delivery.e_status = 'ERROR'
                RETURNING i_delivery
                """
            ),
            {
                "announcement_id": announcement_id,
                "i_push_device": device["i_push_device"],
            },
        ).scalar_one_or_none()
        if row is not None:
            queued.append(
                {
                    "i_delivery": row,
                    "i_push_device": device["i_push_device"],
                    "token": device["e_push_token"],
                }
            )
    db.commit()

    sent = 0
    failed = 0
    for start in range(0, len(queued), 100):
        chunk = queued[start : start + 100]
        messages = [
            {
                "to": item["token"],
                "title": announcement["e_judul"],
                "body": announcement["e_isi"][:200],
                "channelId": "pengumuman_v2",
                "sound": "notification_sound.wav",
                "data": {
                    "type": "announcement",
                    "i_pengumuman": announcement_id,
                },
            }
            for item in chunk
        ]
        try:
            tickets = send_expo_batch(messages)
        except HTTPException:
            db.execute(
                text(
                    """
                    UPDATE public.tm_hr_push_delivery
                    SET e_status = 'ERROR',
                        e_error = 'Expo service unavailable',
                        d_update = now()
                    WHERE i_delivery = ANY(:delivery_ids)
                    """
                ),
                {"delivery_ids": [item["i_delivery"] for item in chunk]},
            )
            db.commit()
            failed += len(chunk)
            continue

        for item, ticket in zip(chunk, tickets):
            ticket_status = ticket.get("status")
            ticket_id = ticket.get("id")
            details = ticket.get("details") or {}
            error_code = details.get("error")
            error = ticket.get("message") or error_code
            if ticket_status == "ok":
                sent += 1
                delivery_status = "TICKETED"
            else:
                failed += 1
                delivery_status = "ERROR"

            db.execute(
                text(
                    """
                    UPDATE public.tm_hr_push_delivery
SET e_status = CAST(:delivery_status AS varchar(20)),
    e_ticket_id = :ticket_id,
    e_error = :error,
    d_sent = CASE
        WHEN CAST(:delivery_status AS varchar(20)) = 'TICKETED'
        THEN now()
        ELSE d_sent
    END,
    d_update = now()
WHERE i_delivery = :i_delivery
                    """
                ),
                {
                    "delivery_status": delivery_status,
                    "ticket_id": ticket_id,
                    "error": error,
                    "i_delivery": item["i_delivery"],
                },
            )
            if error_code == "DeviceNotRegistered":
                db.execute(
                    text(
                        """
                        UPDATE public.tm_hr_push_device
                        SET f_aktif = FALSE,
                            d_update = now()
                        WHERE i_push_device = :i_push_device
                        """
                    ),
                    {"i_push_device": item["i_push_device"]},
                )
        db.commit()

    return {
        "status": True,
        "message": "Proses pengiriman push selesai.",
        "data": {
            "i_pengumuman": announcement_id,
            "eligible_devices": len(devices),
            "queued_now": len(queued),
            "sent": sent,
            "failed": failed,
            "skipped_duplicate": len(devices) - len(queued),
        },
    }
