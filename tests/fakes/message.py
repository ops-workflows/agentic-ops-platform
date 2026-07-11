"""Fake Message REST API.

Implements only the subset the platform uses:

- POST /api/v4/posts                     — create a post
- GET  /api/v4/posts/{thread_id}/thread  — fetch the full thread

Supports pre-scripted human replies injected by tests into a thread. All
received posts are recorded so tests can assert on them.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class _Post(BaseModel):
    id: str
    channel_id: str
    root_id: str = ""
    user_id: str = ""
    username: str = ""
    message: str = ""
    create_at: int = 0
    props: dict[str, Any] = {}


@dataclass
class FakeMattermostState:
    posts: dict[str, _Post] = field(default_factory=dict)
    thread_order: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    posts_by_channel: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    scripted_replies: dict[str, list[_Post]] = field(default_factory=lambda: defaultdict(list))
    lock: Lock = field(default_factory=Lock)
    received_requests: list[dict[str, Any]] = field(default_factory=list)


class FakeMattermost:
    def __init__(self) -> None:
        self.state = FakeMattermostState()
        self.app = self._build_app()

    # ── Test helpers ───────────────────────────────────────────

    def inject_reply(
        self,
        *,
        thread_id: str,
        channel_id: str,
        message: str,
        user_id: str = "user-operator",
        username: str = "operator",
    ) -> _Post:
        with self.state.lock:
            now_ms = int(time.time() * 1000)
            latest_thread_ms = 0
            for post_id in self.state.thread_order.get(thread_id, []):
                post = self.state.posts.get(post_id)
                if post is not None:
                    latest_thread_ms = max(latest_thread_ms, int(post.create_at or 0))
            create_at = max(now_ms, latest_thread_ms + 1)

        post = _Post(
            id=f"reply-{uuid.uuid4().hex[:12]}",
            channel_id=channel_id,
            root_id=thread_id,
            user_id=user_id,
            username=username,
            message=message,
            create_at=create_at,
        )
        with self.state.lock:
            self.state.scripted_replies[thread_id].append(post)
        return post

    def all_posts(self) -> list[_Post]:
        with self.state.lock:
            return list(self.state.posts.values())

    def posts_in_channel(self, channel_id: str) -> list[_Post]:
        with self.state.lock:
            return [self.state.posts[pid] for pid in self.state.posts_by_channel.get(channel_id, [])]

    def posts_in_thread(self, thread_id: str) -> list[_Post]:
        with self.state.lock:
            ids = self.state.thread_order.get(thread_id, [])
            return [self.state.posts[pid] for pid in ids]

    def reset(self) -> None:
        with self.state.lock:
            self.state = FakeMattermostState()

    # ── Synchronous wait helpers (use from sync test helpers) ─

    def wait_for_post(
        self,
        predicate: Callable[[_Post], bool],
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.25,
    ) -> _Post | None:
        """Block until a posted message matches ``predicate`` or timeout.

        Returns the matching post or ``None`` on timeout. Safe to call
        from a background asyncio task — uses busy polling but with a
        configurable interval.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.state.lock:
                posts = list(self.state.posts.values())
            for p in posts:
                if predicate(p):
                    return p
            time.sleep(poll_interval)
        return None

    def post_count(self) -> int:
        with self.state.lock:
            return len(self.state.posts)

    # ── FastAPI app ────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Fake Message")

        @app.post("/api/v4/posts")
        def create_post(body: dict):
            channel_id = str(body.get("channel_id") or "")
            if not channel_id:
                raise HTTPException(400, "channel_id required")
            post_id = f"post-{uuid.uuid4().hex[:12]}"
            post = _Post(
                id=post_id,
                channel_id=channel_id,
                root_id=str(body.get("root_id") or ""),
                user_id="bot-user",
                username="ops-bot",
                message=str(body.get("message") or ""),
                create_at=int(time.time() * 1000),
                props=dict(body.get("props") or {}),
            )
            with self.state.lock:
                self.state.posts[post_id] = post
                self.state.posts_by_channel[channel_id].append(post_id)
                thread_key = post.root_id or post_id
                self.state.thread_order[thread_key].append(post_id)
                self.state.received_requests.append({"op": "create_post", "body": body})
            return post.model_dump()

        @app.get("/api/v4/posts/{thread_id}/thread")
        def get_thread(thread_id: str):
            with self.state.lock:
                self.state.received_requests.append({"op": "get_thread", "thread_id": thread_id})
                scripted = list(self.state.scripted_replies.pop(thread_id, []))
                ids = self.state.thread_order.get(thread_id, [])
                latest_thread_ms = 0
                for post_id in ids:
                    post = self.state.posts.get(post_id)
                    if post is not None:
                        latest_thread_ms = max(latest_thread_ms, int(post.create_at or 0))
                for reply in scripted:
                    # The runtime discards replies whose create_at is <= the
                    # timestamp captured immediately before the approval post.
                    # Materialize scripted replies with a clear delta so they
                    # are always seen as newer than that boundary.
                    latest_thread_ms = max(latest_thread_ms + 1000, int(reply.create_at or 0))
                    reply = reply.model_copy(update={"create_at": latest_thread_ms})
                    self.state.posts[reply.id] = reply
                    self.state.posts_by_channel[reply.channel_id].append(reply.id)
                    self.state.thread_order[thread_id].append(reply.id)

                ids = self.state.thread_order.get(thread_id, [])
                order = [post_id for post_id in ids if post_id in self.state.posts]
                posts = {post_id: self.state.posts[post_id].model_dump() for post_id in order}

            return {"order": order, "posts": posts}

        @app.get("/api/v4/users/me")
        def me():
            return {"id": "bot-user", "username": "ops-bot"}

        @app.get("/_debug/state")
        def debug_state(thread_id: str = ""):
            with self.state.lock:
                posts = {post_id: post.model_dump() for post_id, post in self.state.posts.items()}
                thread_order = dict(self.state.thread_order)
                scripted_replies = {
                    key: [post.model_dump() for post in values] for key, values in self.state.scripted_replies.items()
                }
                if thread_id:
                    order = thread_order.get(thread_id, [])
                    posts = {post_id: posts[post_id] for post_id in order if post_id in posts}
                    thread_order = {thread_id: order}
                    scripted_replies = {thread_id: scripted_replies.get(thread_id, [])}
                return {
                    "posts": posts,
                    "thread_order": thread_order,
                    "scripted_replies": scripted_replies,
                    "received_requests": list(self.state.received_requests),
                }

        @app.get("/_health")
        def health():
            return {"status": "ok"}

        return app


def build_fake_mattermost() -> FakeMattermost:
    return FakeMattermost()
