from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.schemas import AccountCreate, AccountOut, AccountUpdate, AccountWithLimits
from app.models.account import Account, AccountLimits
from app.services.crypto import encrypt

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(get_current_user)])


class BulkAccountItem(BaseModel):
    login: str
    password: str
    totp_secret: str


class BulkAccountRequest(BaseModel):
    tier_id: int
    accounts: list[BulkAccountItem]


class BulkAccountResponse(BaseModel):
    created: int


@router.get("", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Account).order_by(Account.id))
    return result.scalars().all()


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(req: AccountCreate, session: AsyncSession = Depends(get_db_session)):
    account = Account(
        login=req.login,
        password_encrypted=encrypt(req.password),
        totp_secret_encrypted=encrypt(req.totp_secret),
        tier_id=req.tier_id,
        subscription_expires_at=req.subscription_expires_at,
        max_active_rentals=req.max_active_rentals,
        notes=req.notes,
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Login already exists")
    await session.refresh(account)
    return account


@router.post("/bulk", response_model=BulkAccountResponse, status_code=201)
async def bulk_add_accounts(req: BulkAccountRequest, session: AsyncSession = Depends(get_db_session)):
    for item in req.accounts:
        account = Account(
            login=item.login,
            password_encrypted=encrypt(item.password),
            totp_secret_encrypted=encrypt(item.totp_secret),
            tier_id=req.tier_id,
            status="pending_validation",
        )
        session.add(account)
    try:
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
    await session.delete(account)
    await session.commit()
