from __future__ import annotations

import re

REPLY_LIMIT_NOTICE_PATTERNS = [
    re.compile(r"\bonly some accounts can reply\.?", re.IGNORECASE),
    re.compile(
        r"\b(?:post )?author\b.{0,80}\blimit(?:s|ed)?\b.{0,80}\b(?:who can reply|replies?)\.?",
        re.IGNORECASE,
    ),
    re.compile(r"\blimit(?:s|ed)?\b.{0,80}\bwho can reply\.?", re.IGNORECASE),
    re.compile(
        r"\bonly (?:subscribed|subscribers|premium subscribers|verified accounts|accounts from selected regions|"
        r"accounts you mention|accounts you mentioned|your subscribers|your followers|your circle)\b.{0,80}\bcan reply\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\baccounts\b.{0,120}\b(?:following|follows|follow|mentioned|subscribed)\b.{0,120}\bcan reply\.?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bverified accounts or accounts mentioned by @?[A-Za-z0-9_]{1,15}\b.{0,40}\bcan reply\.?",
        re.IGNORECASE,
    ),
]

REPLY_LIMIT_BLOCKED_PATTERNS = [
    re.compile(r"\byou cannot reply to this conversation\.?", re.IGNORECASE),
    re.compile(r"\byou cannot post or quote external links in replies to this post\.?", re.IGNORECASE),
]

RESERVED_PROFILE_PATHS = {
    "",
    "compose",
    "explore",
    "home",
    "i",
    "login",
    "messages",
    "notifications",
    "search",
    "settings",
}

LOGIN_SELECTORS = [
    'input[name="text"]',
    'input[name="password"]',
    "text=Sign in",
    'a[href="/i/flow/login"]',
]

HOME_SELECTORS = [
    'a[data-testid="AppTabBar_Home_Link"][aria-current="page"]',
    'a[href="/home"][aria-current="page"]',
    'a[data-testid="AppTabBar_Home_Link"]',
    'a[href="/home"]',
]

HOME_ENTRY_SELECTORS = [
    'a[data-testid="AppTabBar_Home_Link"]',
    'a[href="/home"]',
    'a[aria-label*="Home"]',
]

HOME_FOR_YOU_TAB_SELECTORS = [
    '[role="tab"]:has-text("For you")',
    'a:has-text("For you")',
    'span:has-text("For you")',
]

HOME_FOLLOWING_TAB_SELECTORS = [
    '[role="tab"]:has-text("Following")',
    'a:has-text("Following")',
    'span:has-text("Following")',
]

PROFILE_ENTRY_SELECTORS = [
    'a[data-testid="AppTabBar_Profile_Link"]',
    'a[aria-label*="Profile"]',
    'a[href^="/"][data-testid="SideNav_AccountSwitcher_Button"]',
]

LOGGED_IN_SELECTORS = [
    '[data-testid="SideNav_AccountSwitcher_Button"]',
    'a[data-testid="AppTabBar_Home_Link"]',
    '[data-testid="SideNav_NewTweet_Button"]',
]

SEARCH_ENTRY_SELECTORS = [
    'a[data-testid="AppTabBar_Explore_Link"]',
    'a[href="/explore"]',
    'a[aria-label*="Search and Explore"]',
]

NOTIFICATIONS_ENTRY_SELECTORS = [
    'a[data-testid="AppTabBar_Notifications_Link"]',
    'a[href="/notifications"]',
    'a[aria-label*="Notifications"]',
]

NOTIFICATIONS_ACTIVE_SELECTORS = [
    'a[data-testid="AppTabBar_Notifications_Link"][aria-current="page"]',
    'a[href="/notifications"][aria-current="page"]',
]

NOTIFICATIONS_MENTIONS_TAB_SELECTORS = [
    'a[href="/notifications/mentions"]',
    '[role="tab"]:has-text("Mentions")',
    'a:has-text("Mentions")',
]

NOTIFICATION_UNREAD_SELECTORS = [
    '[aria-label*="Unread"]',
    '[data-testid*="unread"]',
    '[data-testid*="Unread"]',
]

SEARCH_INPUT_SELECTORS = [
    'input[data-testid="SearchBox_Search_Input"]',
    'input[aria-label="Search query"]',
    'input[placeholder*="Search"]',
]

SEARCH_TAB_LATEST_SELECTORS = [
    'a[href*="&f=live"]',
    'a:has-text("Latest")',
    '[role="tab"]:has-text("Latest")',
]

SEARCH_TAB_TOP_SELECTORS = [
    'a[href*="&f=top"]',
    'a:has-text("Top")',
    '[role="tab"]:has-text("Top")',
]

COMPOSE_BUTTONS = [
    'a[aria-label="Post"]',
    '[href="/compose/tweet"]',
    '[data-testid="SideNav_NewTweet_Button"]',
    '[data-testid="SideNav_Write_Post_Button"]',
]

COMPOSE_TEXTBOXES = [
    '[data-testid="tweetTextarea_0"]',
    '[data-testid="tweetTextarea_1"]',
    'div[role="textbox"]',
    '[contenteditable="true"]',
]

POST_BUTTONS = [
    '[data-testid="tweetButtonInline"]',
    '[data-testid="tweetButton"]',
    'button[data-testid="tweetButton"]',
    'button:has-text("Post")',
    'button:has-text("Reply")',
]

MAX_IMAGES_PER_POST = 4
SUPPORTED_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}

MEDIA_INPUT_SELECTORS = [
    'input[data-testid="fileInput"][type="file"]',
    'input[type="file"][accept*="image"]',
    'input[type="file"]',
]

MEDIA_PREVIEW_SELECTORS = [
    '[data-testid="attachments"]',
    '[data-testid="tweetPhoto"]',
    '[data-testid="media-preview"]',
    'img[alt="Image"]',
    'img[src^="blob:"]',
    'video[src^="blob:"]',
    '[aria-label="Image"]',
    '[aria-label*="Image"]',
]

QUOTE_MENU_ITEMS = [
    '[role="menuitem"]:has-text("Quote")',
    '[role="menuitem"]:has-text("Quote post")',
    '[role="menuitem"]:has-text("Quote Post")',
    'a[href*="/compose/"][href*="tweet_id"]',
    'a[href*="/compose/"][href*="quote"]',
]

LIKE_BUTTONS = [
    '[data-testid="like"]',
    'button[aria-label^="Like"]',
    'button[aria-label*="Like"]',
]

COMMENT_BUTTONS = [
    '[data-testid="reply"]',
    'button[aria-label^="Reply"]',
    'button[aria-label*="Reply"]',
]

POST_MENU_BUTTONS = [
    '[data-testid="caret"]',
    'button[aria-label="More"]',
    'button[aria-label*="More"]',
]

DELETE_MENU_ITEMS = [
    '[role="menuitem"]:has-text("Delete")',
    '[role="menuitem"]:has-text("Delete post")',
    '[role="menuitem"]:has-text("Delete reply")',
]

DELETE_CONFIRM_BUTTONS = [
    '[data-testid="confirmationSheetConfirm"]',
    'button:has-text("Delete")',
    '[role="button"]:has-text("Delete")',
]

BACK_BUTTONS = [
    'button[data-testid="app-bar-back"]',
    'button[aria-label="Back"]',
]

REPLY_SEND_BUTTONS = [
    'button[data-testid="tweetButton"]',
    '[data-testid="tweetButton"]',
    'button[data-testid="tweetButtonInline"]',
    '[data-testid="tweetButtonInline"]',
    'button:has-text("Reply")',
]

REPLY_AUDIENCE_MODAL_SELECTORS = [
    '[role="dialog"]:has([data-testid="app-bar-close"]):has(h2:has-text("Replying to")):has(button:has-text("Done"))',
    '[role="dialog"]:has([data-testid="app-bar-close"]):has(#modal-header):has(button:has-text("Done"))',
]

REPLY_AUDIENCE_DONE_BUTTONS = [
    'button:has-text("Done")',
    '[role="button"]:has-text("Done")',
]

REPLY_CLOSE_BUTTONS = [
    'button[data-testid="app-bar-close"]',
    'button[aria-label="Close"]',
]

LIKE_ACTIVE_BUTTONS = [
    '[data-testid="unlike"]',
    'button[aria-label^="Unlike"]',
    'button[aria-label*="Unlike"]',
]

REPOST_BUTTONS = [
    '[data-testid="retweet"]',
    'button[aria-label^="Repost"]',
    'button[aria-label*="Repost"]',
]

REPOST_ACTIVE_BUTTONS = [
    '[data-testid="unretweet"]',
    'button[aria-label^="Undo repost"]',
    'button[aria-label*="Undo repost"]',
    'button[aria-label^="Reposted"]',
    'button[aria-label*="Reposted"]',
]

UNDO_REPOST_BUTTONS = [
    '[data-testid="unretweetConfirm"]',
    '[role="menuitem"]:has-text("Undo Repost")',
    'button:has-text("Undo Repost")',
    '[role="button"]:has-text("Undo Repost")',
]
