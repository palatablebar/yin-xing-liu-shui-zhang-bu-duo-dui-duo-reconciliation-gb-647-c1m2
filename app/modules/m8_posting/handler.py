"""入账入口：查看凭证、正式入账、中断/旧版恢复。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from . import service

router = APIRouter(prefix="/api/postings", tags=["入账"])


@router.get("")
def get_postings(request: Request):
    store = request.app.state.store
    return service.payload(store, store.latest_run())


@router.post("")
def post_postings(request: Request):
    store = request.app.state.store
    return service.post_journals(store, store.latest_run(), request.app.state.settings)


@router.post("/recover")
def post_recover(request: Request):
    store = request.app.state.store
    return service.recover(store, store.latest_run(), request.app.state.settings)
