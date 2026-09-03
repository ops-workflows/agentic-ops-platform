"""Persisted operational state for external and platform-owned connectors."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shared.lib.models import ConnectorState

MESSAGE_INGRESS_CONNECTOR_ID = "message-ingress"


async def connector_is_paused(session: AsyncSession, connector_id: str) -> bool:
    state = await session.get(ConnectorState, connector_id)
    return state is None or state.paused


async def set_connector_paused(session: AsyncSession, connector_id: str, *, paused: bool) -> ConnectorState:
    state = await session.get(ConnectorState, connector_id)
    if state is None:
        state = ConnectorState(connector_id=connector_id, paused=paused)
        session.add(state)
    else:
        state.paused = paused
    await session.commit()
    return state
