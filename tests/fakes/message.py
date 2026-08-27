"""Fake Mattermost and Slack REST APIs for platform message delivery.

Implements only the subset the platform uses:

- POST /api/v4/posts                     — create a post
- POST /chat.postMessage                 — create a Slack post
- POST /chat.update                      — update a Slack post

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
    lock: Lock = field(default_factory=Lock)
    received_requests: list[dict[str, Any]] = field(default_factory=list)


class FakeMattermost:
    def __init__(self) -> None:
        self.state = FakeMattermostState()
        self.app = self._build_app()

    # ── Test helpers ───────────────────────────────────────────

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

    def seed_post(
        self,
        *,
        post_id: str,
        channel_id: str,
        message: str,
        root_id: str = "",
        user_id: str = "operator-user",
        username: str = "operator",
    ) -> _Post:
        post = _Post(
            id=post_id,
            channel_id=channel_id,
            root_id=root_id,
            user_id=user_id,
            username=username,
            message=message,
            create_at=int(time.time() * 1000),
        )
        with self.state.lock:
            self.state.posts[post_id] = post
            self.state.posts_by_channel[channel_id].append(post_id)
            self.state.thread_order[root_id or post_id].append(post_id)
        return post

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

        @app.get("/api/v4/users/me")
        def me():
            return {"id": "bot-user", "username": "ops-bot"}

        @app.get("/api/v4/posts/{root_id}/thread")
        def mattermost_thread(root_id: str):
            with self.state.lock:
                post_ids = list(self.state.thread_order.get(root_id, []))
                posts = {post_id: self.state.posts[post_id].model_dump() for post_id in post_ids}
                self.state.received_requests.append({"op": "mattermost_thread", "root_id": root_id})
            return {"order": post_ids, "posts": posts}

        @app.post("/chat.postMessage")
        def slack_create_post(body: dict):
            channel_id = str(body.get("channel") or "")
            if not channel_id:
                return {"ok": False, "error": "channel_not_found"}
            post_id = f"{time.time():.6f}"
            post = _Post(
                id=post_id,
                channel_id=channel_id,
                root_id=str(body.get("thread_ts") or ""),
                user_id="slack-bot-user",
                username="ops-bot",
                message=str(body.get("text") or ""),
                create_at=int(time.time() * 1000),
                props={"blocks": list(body.get("blocks") or [])},
            )
            with self.state.lock:
                self.state.posts[post_id] = post
                self.state.posts_by_channel[channel_id].append(post_id)
                self.state.thread_order[post.root_id or post_id].append(post_id)
                self.state.received_requests.append({"op": "slack_create_post", "body": body})
            return {"ok": True, "channel": channel_id, "ts": post_id, "message": {"ts": post_id}}

        @app.post("/chat.update")
        def slack_update_post(body: dict):
            post_id = str(body.get("ts") or "")
            with self.state.lock:
                existing = self.state.posts.get(post_id)
                if existing is None:
                    return {"ok": False, "error": "message_not_found"}
                updated = existing.model_copy(
                    update={"message": str(body.get("text") or ""), "props": {"blocks": list(body.get("blocks") or [])}}
                )
                self.state.posts[post_id] = updated
                self.state.received_requests.append({"op": "slack_update_post", "body": body})
            return {"ok": True, "channel": existing.channel_id, "ts": post_id}

        @app.post("/auth.test")
        def slack_auth_test():
            return {"ok": True, "user_id": "slack-bot-user"}

        @app.post("/conversations.replies")
        def slack_thread(body: dict):
            thread_id = str(body.get("ts") or "")
            channel_id = str(body.get("channel") or "")
            with self.state.lock:
                post_ids = list(self.state.thread_order.get(thread_id, []))
                posts = [self.state.posts[post_id] for post_id in post_ids]
                self.state.received_requests.append({"op": "slack_thread", "body": body})
            messages = [
                {
                    "ts": post.id,
                    "user": post.user_id,
                    "username": post.username,
                    "text": post.message,
                }
                for post in posts
                if post.channel_id == channel_id
            ]
            return {"ok": True, "messages": messages, "response_metadata": {"next_cursor": ""}}

        @app.get("/conversations.list")
        def slack_list_channels():
            return {
                "ok": True,
                "channels": [{"id": "C123", "name": "platform-test-channel"}],
                "response_metadata": {"next_cursor": ""},
            }

        @app.get("/_debug/state")
        def debug_state(thread_id: str = ""):
            with self.state.lock:
                posts = {post_id: post.model_dump() for post_id, post in self.state.posts.items()}
                thread_order = dict(self.state.thread_order)
                if thread_id:
                    order = thread_order.get(thread_id, [])
                    posts = {post_id: posts[post_id] for post_id in order if post_id in posts}
                    thread_order = {thread_id: order}
                return {
                    "posts": posts,
                    "thread_order": thread_order,
                    "received_requests": list(self.state.received_requests),
                }

        @app.get("/_health")
        def health():
            return {"status": "ok"}

        return app


def build_fake_mattermost() -> FakeMattermost:
    return FakeMattermost()
