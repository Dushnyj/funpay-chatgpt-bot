from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import AccountCreate, AccountOut, AccountUpdate, AccountWithLimits
from app.check_job_queue import CheckJobQueue
from app.models.account import Account, AccountLimits
from app.models.audit import AuditLog
from app.models.catalog import SubscriptionTier
from app.models.rental import Rental

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(get_current_user)])
_check_job_queue = CheckJobQueue()


class BulkAccountItem(BaseModel):
    login: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=4096)
    totp_secret: str = Field(default="", max_length=256)
    email: str | None = Field(default=None, max_length=320)
    email_password: str | None = Field(default=None, max_length=4096)


class BulkAccountRequest(BaseModel):
    tier_id: int = Field(gt=0)
    accounts: list[BulkAccountItem] = Field(min_length=1, max_length=1000)


class BulkAccountResponse(BaseModel):
    created: int


@router.get("", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Account).order_by(Account.id))
    return result.scalars().all()


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(req: AccountCreate, session: AsyncSession = Depends(get_db_session)):
    if await session.get(SubscriptionTier, req.tier_id) is None:
        raise HTTPException(status_code=422, detail="Unknown subscription tier")
    account = Account(
        login=req.login,
        password_encrypted=req.password,
        totp_secret_encrypted=req.totp_secret,
        email=req.email,
        email_password_encrypted=req.email_password,
        tier_id=req.tier_id,
        subscription_expires_at=req.subscription_expires_at,
        max_active_rentals=req.max_active_rentals,
        notes=req.notes,
    )
    session.add(account)
    try:
        await session.flush()
        await _check_job_queue.enqueue(
            session, account.id, priority="new", job_type="full_validation"
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Login already exists")
    await session.refresh(account)
    return account


@router.post("/bulk", response_model=BulkAccountResponse, status_code=201)
async def bulk_add_accounts(req: BulkAccountRequest, session: AsyncSession = Depends(get_db_session)):
    if await session.get(SubscriptionTier, req.tier_id) is None:
        raise HTTPException(status_code=422, detail="Unknown subscription tier")
    accounts: list[Account] = []
    for item in req.accounts:
        account = Account(
            login=item.login,
            password_encrypted=item.password,
            totp_secret_encrypted=item.totp_secret,
            email=item.email,
            email_password_encrypted=item.email_password,
            tier_id=req.tier_id,
            status="pending_validation",
        )
        session.add(account)
        accounts.append(account)
    try:
        await session.flush()
        for account in accounts:
            await _check_job_queue.enqueue(
                session, account.id, priority="new", job_type="full_validation"
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Duplicate login")
    return BulkAccountResponse(created=len(req.accounts))


@router.get("/{account_id}", response_model=AccountWithLimits)
async def get_account(account_id: int, session: AsyncSession = Depends(get_db_session)):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    limits = await session.get(AccountLimits, account_id)
    return AccountWithLimits(
        id=account.id, login=account.login, tier_id=account.tier_id,
        email=account.email,
        subscription_expires_at=account.subscription_expires_at,
        max_active_rentals=account.max_active_rentals,
        status=account.status, notes=account.notes,
        limits=limits,
    )


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(account_id: int, req: AccountUpdate, session: AsyncSession = Depends(get_db_session)):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await session.commit()
    await session.refresh(account)
    return account


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, session: AsyncSession = Depends(get_db_session)):
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    active_rental = await session.scalar(
        select(Rental.id).where(
            Rental.account_id == account_id,
            Rental.status == "active",
        ).limit(1)
    )
    if active_rental is not None:
        raise HTTPException(status_code=409, detail="Account has an active rental")
    await session.delete(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Account is referenced by history")


class TotpExportResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_png_base64: str


@router.get("/{account_id}/totp-export", response_model=TotpExportResponse)
async def export_totp(
    account_id: int,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    """Экспорт TOTP: secret, otpauth:// URI, QR-код (base64 PNG) для импорта в приложение."""
    from app.services.otp_export import generate_otpauth_uri, generate_qr_base64
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    secret = account.totp_secret_encrypted or ""
    if not secret:
        raise HTTPException(status_code=400, detail="Account has no TOTP secret")

    account_name = account.email or account.login
    uri = generate_otpauth_uri(secret, account_name)
    qr_b64 = generate_qr_base64(uri)
    session.add(
        AuditLog(
            event_type="totp_export",
            account_id=account.id,
            metadata_={"actor": "admin"},
        )
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TotpExportResponse(secret=secret, otpauth_uri=uri, qr_png_base64=qr_b64)

