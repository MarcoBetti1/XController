from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

try:
    from XController import XTextAdapter
except ModuleNotFoundError as exc:
    if exc.name != "XController":
        raise
    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "XController",
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    if spec is None or spec.loader is None:
        raise
    module = importlib.util.module_from_spec(spec)
    sys.modules["XController"] = module
    spec.loader.exec_module(module)
    from XController import XTextAdapter


class NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None


class FakeLocator:
    def __init__(self, items: list[FakeNode]):
        self.items = items

    @property
    def first(self) -> FakeNode:
        return self.items[0] if self.items else FakeMissing()

    def nth(self, idx: int) -> FakeNode:
        return self.items[idx]

    async def count(self) -> int:
        return len(self.items)


class FakeNode:
    def __init__(self, text: str = "", attrs: dict[str, str] | None = None):
        self.text = text
        self.attrs = attrs or {}

    @property
    def first(self) -> FakeNode:
        return self

    def nth(self, _idx: int) -> FakeNode:
        return self

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator([])

    async def count(self) -> int:
        return 1

    async def get_attribute(self, attr: str) -> str | None:
        return self.attrs.get(attr)

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.text


class FakeMissing(FakeNode):
    async def count(self) -> int:
        return 0


class FakeArticle(FakeNode):
    def __init__(
        self,
        *,
        actor: str,
        post_id: str,
        tweet_text: str,
        body: str,
        notification_type_text: str,
        unread: bool = False,
        created_at: str = "2026-04-21T15:00:00Z",
        status_links: list[str] | None = None,
    ):
        super().__init__(text=body, attrs={"aria-label": "Unread notification" if unread else ""})
        self.actor = actor
        self.post_id = post_id
        self.tweet_text = tweet_text
        self.notification_type_text = notification_type_text
        self.unread = unread
        self.created_at = created_at
        self.status_links = status_links

    def locator(self, selector: str) -> FakeLocator:
        if selector == 'a[href*="/status/"]':
            status_links = self.status_links or [f"/{self.actor}/status/{self.post_id}"]
            return FakeLocator([FakeNode(attrs={"href": href}) for href in status_links])
        if selector == 'a[href^="/"]':
            return FakeLocator(
                [
                    FakeNode(attrs={"href": f"/{self.actor}"}),
                    FakeNode(attrs={"href": f"/{self.actor}/status/{self.post_id}"}),
                ]
            )
        if selector == 'div[data-testid="User-Name"] a[href^="/"]':
            return FakeLocator([FakeNode(attrs={"href": f"/{self.actor}"})])
        if selector == 'div[data-testid="User-Name"]':
            return FakeLocator([FakeNode(text=f"{self.actor}\n@{self.actor}")])
        if selector == 'div[data-testid="tweetText"]':
            return FakeLocator([FakeNode(text=self.tweet_text)])
        if selector == '[data-testid="socialContext"]':
            return FakeLocator([FakeNode(text=self.notification_type_text)])
        if selector == "time":
            return FakeLocator([FakeNode(attrs={"datetime": self.created_at})])
        if "Unread" in selector or "unread" in selector:
            return FakeLocator([FakeNode(text="Unread")]) if self.unread else FakeLocator([])
        return FakeLocator([])


class FakePage:
    url = "https://x.com/notifications"

    def __init__(self, articles: list[FakeArticle]):
        self.articles = articles

    def locator(self, selector: str) -> FakeLocator:
        if selector in {'article[data-testid="tweet"], article', "article"}:
            return FakeLocator(self.articles)
        return FakeLocator([])


class NotificationReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = NoopHuman()
        self.adapter._random_scroll = AsyncMock()
        self.adapter._looks_like_notifications_page = AsyncMock(return_value=True)

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    async def test_read_notifications_returns_exact_requested_limit(self) -> None:
        self.adapter.page = FakePage(
            [
                FakeArticle(
                    actor="alice",
                    post_id="101",
                    tweet_text="First post",
                    body="Alice liked your post\nFirst post\n2 likes",
                    notification_type_text="Alice liked your post",
                ),
                FakeArticle(
                    actor="bob",
                    post_id="102",
                    tweet_text="Second post",
                    body="Bob replied to your post\nSecond post",
                    notification_type_text="Bob replied to your post",
                ),
                FakeArticle(
                    actor="carol",
                    post_id="103",
                    tweet_text="Third post",
                    body="Carol reposted your post\nThird post",
                    notification_type_text="Carol reposted your post",
                ),
            ]
        )

        notifications = await self.adapter.read_notifications(limit=2)

        self.assertEqual(len(notifications), 2)
        self.assertEqual([item.platform_post_id for item in notifications], ["101", "102"])
        self.assertEqual(notifications[0].notification_type, "like")
        self.assertEqual(notifications[0].actor, "alice")
        self.assertFalse(notifications[0].unread)
        self.assertEqual(notifications[0].raw["metrics"]["likes"], 2)

    async def test_read_notifications_unread_only_filters_read_cards(self) -> None:
        self.adapter.page = FakePage(
            [
                FakeArticle(
                    actor="alice",
                    post_id="101",
                    tweet_text="Read post",
                    body="Alice liked your post\nRead post",
                    notification_type_text="Alice liked your post",
                    unread=False,
                ),
                FakeArticle(
                    actor="bob",
                    post_id="102",
                    tweet_text="Unread post",
                    body="Bob mentioned you\nUnread post",
                    notification_type_text="Bob mentioned you",
                    unread=True,
                ),
            ]
        )

        notifications = await self.adapter.read_notifications(limit=1, unread_only=True)

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].platform_post_id, "102")
        self.assertTrue(notifications[0].unread)
        self.assertEqual(notifications[0].notification_type, "mention")

    async def test_notification_raw_includes_related_status_ids(self) -> None:
        self.adapter.page = FakePage(
            [
                FakeArticle(
                    actor="bob",
                    post_id="202",
                    tweet_text="Reply post",
                    body="Bob replied to your post\nReply post",
                    notification_type_text="Bob replied to your post",
                    status_links=[
                        "/bob/status/202",
                        "/PixelGamingCo/status/101",
                        "https://x.com/bob/status/202",
                    ],
                ),
            ]
        )

        notifications = await self.adapter.read_notifications(limit=1)

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].platform_post_id, "202")
        self.assertEqual(notifications[0].raw["status_post_ids"], ["202", "101"])
        self.assertEqual(notifications[0].raw["related_post_ids"], ["101"])
        self.assertEqual(
            notifications[0].raw["status_urls"],
            [
                "https://x.com/bob/status/202",
                "https://x.com/PixelGamingCo/status/101",
            ],
        )


class PostRestrictionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = NoopHuman()
        self.adapter._random_scroll = AsyncMock()

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    async def test_read_visible_posts_flags_author_reply_limit_notice(self) -> None:
        self.adapter.page = FakePage(
            [
                FakeArticle(
                    actor="marco_ocram10",
                    post_id="2046694255486230759",
                    tweet_text="T",
                    body="Macro\n@marco_ocram10\nT\nOnly some accounts can reply.\n5 Views",
                    notification_type_text="",
                ),
            ]
        )

        posts = await self.adapter.read_visible_posts(limit=1)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].text, "T")
        self.assertTrue(posts[0].author_limited)
        self.assertTrue(posts[0].reply_limited)
        self.assertEqual(posts[0].author_limit_notice, "Only some accounts can reply.")
        self.assertEqual(posts[0].raw["author_limit_type"], "reply")

    async def test_article_metrics_ignore_metric_words_in_post_text(self) -> None:
        article = FakeArticle(
            actor="AnantapurBO",
            post_id="2051882249730347307",
            tweet_text=(
                "110M views\n"
                "6.4M likes\n\n"
                "@hegdepooja's Highest liked post on @instagram ? #TVKVijay #TVKVijayHQ #TVK"
            ),
            body=(
                "ATPBO\n"
                "@AnantapurBO\n"
                "·\n"
                "9h\n"
                "110M views\n"
                "6.4M likes\n\n"
                "@hegdepooja's Highest liked post on @instagram ? #TVKVijay #TVKVijayHQ #TVK\n"
                "4\n"
                "30\n"
                "91\n"
                "1.5K"
            ),
            notification_type_text="",
        )

        metrics = await self.adapter._extract_article_metrics(article)

        self.assertEqual(metrics["replies"], 4)
        self.assertEqual(metrics["comments"], 4)
        self.assertEqual(metrics["reposts"], 30)
        self.assertEqual(metrics["likes"], 91)
        self.assertEqual(metrics["views"], 1500)

    async def test_article_metrics_use_footer_order_for_unlabeled_counts(self) -> None:
        article = FakeArticle(
            actor="drpezeshkian",
            post_id="2051681840218710032",
            tweet_text=(
                "If politics is reduced to power, the result is today's world: chaos, oppression, injustice, and piracy\n\n"
                "In our national ethos and religious worldview, power without ethics is hollow. Today، Iran represents "
                "ethical, responsible power; its enemies embody reckless & unchecked force."
            ),
            body=(
                "Masoud Pezeshkian\n"
                "@drpezeshkian\n"
                "·\n"
                "May 5\n"
                "If politics is reduced to power, the result is today's world: chaos, oppression, injustice, and piracy\n\n"
                "In our national ethos and religious worldview, power without ethics is hollow. Today، Iran represents "
                "ethical, responsible power; its enemies embody reckless & unchecked force.\n"
                "999\n"
                "3.2K\n"
                "13K\n"
                "401K"
            ),
            notification_type_text="",
        )

        metrics = await self.adapter._extract_article_metrics(article)

        self.assertEqual(metrics["replies"], 999)
        self.assertEqual(metrics["comments"], 999)
        self.assertEqual(metrics["reposts"], 3200)
        self.assertEqual(metrics["likes"], 13000)
        self.assertEqual(metrics["views"], 401000)

    async def test_article_metrics_ignore_status_ids_before_three_number_footer(self) -> None:
        article = FakeArticle(
            actor="45johnmac",
            post_id="2050495751692632555",
            tweet_text="America has deadly preservatives and carcinogenic ingredients in food.",
            body=(
                "Gordon Master\n"
                "@45johnmac\n"
                "America has deadly preservatives and carcinogenic ingredients in food.\n"
                "Quote\n"
                "Other User\n"
                "https://\n"
                "x.com/i/status/20371\n"
                "20770434850821\n"
                "...\n"
                "16\n"
                "20\n"
                "201"
            ),
            notification_type_text="",
        )

        metrics = await self.adapter._extract_article_metrics(article)

        self.assertEqual(metrics["replies"], 0)
        self.assertEqual(metrics["comments"], 0)
        self.assertEqual(metrics["reposts"], 16)
        self.assertEqual(metrics["likes"], 20)
        self.assertEqual(metrics["views"], 201)

    async def test_article_metrics_ignore_quote_card_timestamps_before_footer(self) -> None:
        article = FakeArticle(
            actor="visaraj",
            post_id="2051988242300563529",
            tweet_text="Look at the numbers in south. Literally a washout.",
            body=(
                "Narayanan\n"
                "@visaraj\n"
                "51m\n"
                "Look at the numbers in south. Literally a washout.\n"
                "Quote\n"
                "Sujaya Krishna\n"
                "@sujayak\n"
                "58m\n"
                "Replying to @visaraj\n"
                "I have seen the numbers. It does not look as bad.\n"
                "2\n"
                "9\n"
                "368"
            ),
            notification_type_text="",
        )

        metrics = await self.adapter._extract_article_metrics(article)

        self.assertEqual(metrics["replies"], 0)
        self.assertEqual(metrics["comments"], 0)
        self.assertEqual(metrics["reposts"], 2)
        self.assertEqual(metrics["likes"], 9)
        self.assertEqual(metrics["views"], 368)

    async def test_article_metrics_split_line_views_do_not_become_reposts(self) -> None:
        article = FakeArticle(
            actor="PixelGamingCo",
            post_id="2057148652787765310",
            tweet_text="all that hype is not free, somebody is eating the cost",
            body=(
                "PixelGamingCo\n"
                "@PixelGamingCo\n"
                "all that hype is not free, somebody is eating the cost\n"
                "0\n"
                "2,627\n"
                "Views\n"
                "14\n"
                "Likes\n"
                "2,627\n"
                "Views"
            ),
            notification_type_text="",
        )

        metrics = await self.adapter._extract_article_metrics(article)

        self.assertEqual(metrics["replies"], 0)
        self.assertEqual(metrics["comments"], 0)
        self.assertEqual(metrics["reposts"], 0)
        self.assertEqual(metrics["likes"], 14)
        self.assertEqual(metrics["views"], 2627)

    async def test_notifications_include_author_reply_limit_notice(self) -> None:
        self.adapter.page = FakePage(
            [
                FakeArticle(
                    actor="alice",
                    post_id="201",
                    tweet_text="Thanks @me",
                    body="Alice mentioned you\nThanks @me\nAccounts @alice mentioned can reply",
                    notification_type_text="Alice mentioned you",
                ),
            ]
        )

        notification = await self.adapter._extract_notification_from_article(self.adapter.page.articles[0])

        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertTrue(notification.author_limited)
        self.assertTrue(notification.reply_limited)
        self.assertEqual(notification.raw["author_limit_type"], "reply")
        self.assertEqual(notification.author_limit_notice, "Accounts @alice mentioned can reply")

    def test_author_limit_notice_accepts_author_wording(self) -> None:
        state = self.adapter._extract_article_author_limit_state(
            "The post author limits who can reply."
        )

        self.assertTrue(state["author_limited"])
        self.assertTrue(state["reply_limited"])
        self.assertEqual(state["author_limit_type"], "reply")
        self.assertEqual(state["author_limit_notice"], "post author limits who can reply.")


if __name__ == "__main__":
    unittest.main()
