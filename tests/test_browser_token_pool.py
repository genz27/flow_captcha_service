import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.browser_captcha import BrowserCaptchaService, BrowserProfile, StandbyTokenEntry, TokenBrowser


class BrowserTokenPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()
        self.bucket_key = "project-a|IMAGE_GENERATION|-"

    async def test_expired_entry_does_not_hit(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="expired-token",
                browser_id=1,
                fingerprint={"user_agent": "ua-expired"},
                browser_epoch=3,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value - 10,
                expires_monotonic=now_value - 1,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=3):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNone(result)
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)

    async def test_hit_pops_entry_from_pool(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="warm-token",
                browser_id=2,
                fingerprint={"user_agent": "ua-live"},
                browser_epoch=5,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value,
                expires_monotonic=now_value + 30,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=5):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNotNone(result)
        self.assertEqual(result.token, "warm-token")
        self.assertEqual(result.browser_ref, 2)
        self.assertEqual(result.browser_epoch, 5)
        self.assertEqual(result.fingerprint, {"user_agent": "ua-live"})
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)

    async def test_epoch_mismatch_invalidates_entry(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="stale-token",
                browser_id=4,
                fingerprint={"user_agent": "ua-stale"},
                browser_epoch=7,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value,
                expires_monotonic=now_value + 30,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=8):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNone(result)
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)

    async def test_missing_browser_epoch_keeps_entry_usable(self):
        now_value = time.monotonic()
        self.service._standby_tokens[self.bucket_key] = [
            StandbyTokenEntry(
                token="warm-token",
                browser_id=5,
                fingerprint={"user_agent": "ua-live"},
                browser_epoch=9,
                project_id="project-a",
                action="IMAGE_GENERATION",
                proxy_signature="-",
                created_monotonic=now_value,
                expires_monotonic=now_value + 30,
            )
        ]

        with patch.object(self.service, "_get_browser_epoch_for_standby", return_value=None):
            result = await self.service._take_standby_token(self.bucket_key)

        self.assertIsNotNone(result)
        self.assertEqual(result.token, "warm-token")
        self.assertEqual(result.browser_ref, 5)
        self.assertNotIn(self.bucket_key, self.service._standby_tokens)

    async def test_custom_token_uses_shared_browser_path(self):
        browser = TokenBrowser(3, "tmp/test-custom-token-shared")
        fake_context = object()

        with patch.object(
            browser,
            "_get_or_create_shared_browser",
            AsyncMock(return_value=(object(), object(), fake_context)),
        ) as shared_browser_mock:
            with patch.object(
                browser,
                "_create_browser",
                AsyncMock(side_effect=AssertionError("should not create temporary browser")),
            ):
                with patch.object(
                    browser,
                    "_execute_custom_captcha",
                    AsyncMock(return_value="shared-custom-token"),
                ) as execute_mock:
                    token = await browser.get_custom_token(
                        website_url="https://example.com/login",
                        website_key="site-key",
                        action="login",
                    )

        self.assertEqual(token, "shared-custom-token")
        shared_browser_mock.assert_awaited_once()
        execute_mock.assert_awaited_once()
        self.assertTrue(bool(execute_mock.await_args.kwargs["reuse_ready_page"]))

    async def test_custom_page_cache_hits_same_site(self):
        browser = TokenBrowser(4, "tmp/test-custom-page-cache")
        website_url = "https://example.com/login"
        website_key = "site-key"
        custom_key = browser._build_custom_page_key(
            website_url=website_url,
            website_key=website_key,
            captcha_type="recaptcha_v3",
            enterprise=False,
        )

        class FakePage:
            def is_closed(self):
                return False

            async def evaluate(self, _expression):
                return True

        fake_page = FakePage()
        browser._shared_custom_pages[custom_key] = fake_page
        browser._shared_custom_page_last_used[custom_key] = time.monotonic()

        class FakeContext:
            async def new_page(self):
                raise AssertionError("cache hit should not create a new page")

        page, resolved_key, runtime, ready_hit = await browser._get_or_create_custom_page(
            FakeContext(),
            website_url=website_url,
            website_key=website_key,
            captcha_type="recaptcha_v3",
            enterprise=False,
        )

        self.assertIs(page, fake_page)
        self.assertEqual(resolved_key, custom_key)
        self.assertEqual(runtime["normalized_type"], "recaptcha_v3")
        self.assertTrue(ready_hit)

    async def test_custom_page_cache_evicts_stale_entries(self):
        browser = TokenBrowser(5, "tmp/test-custom-page-cache-stale")

        class FakePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True

        stale_page = FakePage()
        hot_page = FakePage()
        browser._shared_custom_pages = {"stale": stale_page, "hot": hot_page}
        browser._shared_custom_page_last_used = {"stale": 1.0, "hot": time.monotonic()}

        with patch.object(browser, "_custom_page_idle_ttl_seconds", return_value=0.01):
            await browser._trim_shared_custom_pages(keep_key="hot", max_pages=2)

        self.assertNotIn("stale", browser._shared_custom_pages)
        self.assertTrue(stale_page.closed)
        self.assertIn("hot", browser._shared_custom_pages)

    async def test_user_agent_pool_expanded_by_one_hundred(self):
        expected_total = len(TokenBrowser._BASE_UA_LIST) + TokenBrowser.UA_POOL_EXTRA_COUNT
        self.assertEqual(len(TokenBrowser.UA_LIST), expected_total)
        self.assertEqual(len(TokenBrowser.UA_LIST), len(set(TokenBrowser.UA_LIST)))
        browser = TokenBrowser(51, "tmp/test-default-profile-pool")
        self.assertEqual(len(browser._profile_pool), expected_total)

    async def test_profile_pool_honors_configured_extra_count(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(browser_fingerprint_pool_extra_count=5),
        ):
            browser = TokenBrowser(6, "tmp/test-profile-pool-extra")

        self.assertEqual(len(browser._profile_pool), len(TokenBrowser._BASE_UA_LIST) + 5)

    async def test_default_profile_pool_is_reused_across_browsers(self):
        browser_a = TokenBrowser(61, "tmp/test-profile-pool-reuse-a")
        browser_b = TokenBrowser(62, "tmp/test-profile-pool-reuse-b")

        self.assertIs(browser_a._profile_pool, browser_b._profile_pool)
        self.assertIs(browser_a._profile_pool, TokenBrowser.DEFAULT_PROFILE_POOL)

    async def test_profile_pool_cache_is_reused_for_same_extra_count(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(browser_fingerprint_pool_extra_count=5),
        ):
            browser_a = TokenBrowser(63, "tmp/test-profile-pool-cache-a")
            browser_b = TokenBrowser(64, "tmp/test-profile-pool-cache-b")

        self.assertIs(browser_a._profile_pool, browser_b._profile_pool)

    async def test_profile_pool_allows_zero_extra_count(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(browser_fingerprint_pool_extra_count=0),
        ):
            browser = TokenBrowser(6, "tmp/test-profile-pool-zero-extra")

        self.assertEqual(len(browser._profile_pool), len(TokenBrowser._BASE_UA_LIST))

    async def test_refresh_browser_profile_keeps_mobile_profile_shape(self):
        browser = TokenBrowser(7, "tmp/test-mobile-profile")
        browser._profile_pool = [
            BrowserProfile(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
                viewport={"width": 430, "height": 932},
                locale="en-US",
                timezone_id="America/Los_Angeles",
                accept_language="en-US,en;q=0.9",
                device_scale_factor=3.0,
                is_mobile=True,
                has_touch=True,
                profile_family="mobile",
            )
        ]

        browser._refresh_browser_profile()

        self.assertTrue(browser._profile_is_mobile)
        self.assertTrue(browser._profile_has_touch)
        self.assertEqual(browser._profile_viewport, {"width": 430, "height": 932})
        self.assertEqual(browser._profile_timezone_id, "America/Los_Angeles")

    async def test_custom_page_cache_uses_configured_limits(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(
                browser_custom_page_cache_max_pages=7,
                browser_custom_page_idle_ttl_seconds=90,
            ),
        ):
            browser = TokenBrowser(8, "tmp/test-custom-page-config")
            self.assertEqual(browser._custom_page_cache_max_pages(), 7)
            self.assertEqual(browser._custom_page_idle_ttl_seconds(), 90.0)

    async def test_service_custom_token_uses_site_affinity_slot_selection(self):
        service = BrowserCaptchaService()

        class FakeBrowser:
            def __init__(self):
                self.get_custom_token = AsyncMock(return_value="service-token")

        fake_browser = FakeBrowser()

        with patch.object(service, "_check_available"):
            with patch.object(service, "_resolve_global_proxy_url", AsyncMock(return_value=None)):
                with patch.object(service, "_select_browser_id", AsyncMock(return_value=2)) as select_mock:
                    with patch.object(service, "_get_next_browser_id", side_effect=AssertionError("should not use round robin")):
                        with patch.object(service, "_get_or_create_browser", AsyncMock(return_value=fake_browser)):
                            token, browser_id = await service.get_custom_token(
                                website_url="https://example.com/login",
                                website_key="site-key",
                                action="login",
                                captcha_type="recaptcha_v3",
                            )

        self.assertEqual(token, "service-token")
        self.assertEqual(browser_id, 2)
        select_mock.assert_awaited_once()

    async def test_refill_prefers_other_idle_browser_when_preferred_busy(self):
        service = BrowserCaptchaService()
        service._browser_count = 3

        class FakeBrowser:
            def __init__(self, busy):
                self._busy = busy

            def is_busy(self):
                return self._busy

        service._browsers = {
            0: FakeBrowser(True),
            1: FakeBrowser(False),
        }

        selected = await service._select_idle_browser_id_for_refill(
            project_id="project-a",
            preferred_browser_id=0,
        )

        self.assertEqual(selected, 1)

    async def test_project_affinity_trim_evicts_old_keys(self):
        service = BrowserCaptchaService()
        service._project_slot_affinity = {
            "old-1": [0],
            "old-2": [1],
            "keep": [0],
        }
        service._project_slot_last_used = {
            "old-1": 1.0,
            "old-2": 2.0,
            "keep": time.monotonic(),
        }

        with patch.object(service, "_project_affinity_max_keys", return_value=1):
            async with service._project_slot_lock:
                service._trim_project_affinity_locked()

        self.assertEqual(service._project_slot_affinity, {"keep": [0]})

    async def test_standby_bucket_trim_evicts_old_buckets(self):
        service = BrowserCaptchaService()
        now_value = time.monotonic()
        service._standby_tokens = {
            "old": [
                StandbyTokenEntry(
                    token="token-old",
                    browser_id=1,
                    fingerprint={"user_agent": "ua-old"},
                    browser_epoch=1,
                    project_id="p-old",
                    action="IMAGE_GENERATION",
                    proxy_signature="-",
                    created_monotonic=now_value,
                    expires_monotonic=now_value + 30,
                )
            ],
            "new": [
                StandbyTokenEntry(
                    token="token-new",
                    browser_id=2,
                    fingerprint={"user_agent": "ua-new", "extra": "drop-me"},
                    browser_epoch=1,
                    project_id="p-new",
                    action="IMAGE_GENERATION",
                    proxy_signature="-",
                    created_monotonic=now_value,
                    expires_monotonic=now_value + 30,
                )
            ],
        }
        service._standby_bucket_last_used = {"old": 1.0, "new": now_value}

        with patch.object(service, "_standby_bucket_max_count", return_value=1):
            with patch.object(service, "_is_standby_entry_valid", return_value=True):
                async with service._standby_lock:
                    cancelled = service._trim_standby_buckets_locked(now_value=now_value)

        self.assertEqual(cancelled, [])
        self.assertNotIn("old", service._standby_tokens)
        self.assertIn("new", service._standby_tokens)

    async def test_service_limits_use_configured_values(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(
                browser_project_affinity_max_keys=5,
                browser_project_affinity_ttl_seconds=120,
                browser_standby_bucket_max_count=9,
                browser_standby_bucket_idle_ttl_seconds=150,
                browser_idle_reaper_interval_seconds=4,
            ),
        ):
            service = BrowserCaptchaService()
            self.assertEqual(service._project_affinity_max_keys(), 5)
            self.assertEqual(service._project_affinity_ttl_seconds(), 120.0)
            self.assertEqual(service._standby_bucket_max_count(), 9)
            self.assertEqual(service._standby_bucket_idle_ttl_seconds(), 150.0)
            self.assertEqual(service._idle_reaper_interval_seconds(), 4.0)

    async def test_service_limits_keep_auto_fallback_when_zero_configured(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(
                browser_project_affinity_max_keys=0,
                browser_project_affinity_ttl_seconds=120,
                browser_standby_bucket_max_count=0,
                browser_standby_bucket_idle_ttl_seconds=0,
                browser_standby_token_ttl_seconds=150,
                browser_idle_reaper_interval_seconds=4,
            ),
        ):
            service = BrowserCaptchaService()
            service._browser_count = 4
            self.assertEqual(service._project_affinity_max_keys(), 64)
            self.assertEqual(service._project_affinity_ttl_seconds(), 120.0)
            self.assertEqual(service._standby_bucket_max_count(), 48)
            self.assertEqual(service._standby_bucket_idle_ttl_seconds(), 300.0)
            self.assertEqual(service._idle_reaper_interval_seconds(), 4.0)

    async def test_browser_request_finish_and_execute_timeout_support_auto_mode(self):
        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(
                browser_execute_timeout_seconds=0,
                browser_request_finish_image_wait_seconds=0,
                browser_request_finish_non_image_wait_seconds=0,
                browser_retry_backoff_seconds=0,
            ),
        ):
            browser = TokenBrowser(9, "tmp/test-auto-timeouts")
            self.assertEqual(browser._execute_timeout_seconds(fallback=30.0), 30.0)
            self.assertEqual(browser._execute_timeout_seconds(fallback=45.0), 45.0)
            self.assertEqual(browser._retry_backoff_seconds(), 0.0)
            self.assertEqual(
                browser._request_finish_image_wait_seconds(flow_timeout=600, upsample_timeout=800),
                980,
            )
            self.assertEqual(
                browser._request_finish_non_image_wait_seconds(flow_timeout=600),
                1800,
            )

    async def test_store_standby_token_compacts_fingerprint(self):
        service = BrowserCaptchaService()
        result = type("Result", (), {})()
        result.token = "standby-token"
        result.browser_id = 7
        result.browser_epoch = 3
        result.fingerprint = {
            "user_agent": "ua-live",
            "accept_language": "zh-CN",
            "big_blob": "x" * 128,
        }

        await service._store_standby_token(
            "bucket-a",
            result,
            project_id="project-a",
            action="IMAGE_GENERATION",
        )

        stored = service._standby_tokens["bucket-a"][0]
        self.assertEqual(stored.fingerprint, {"user_agent": "ua-live", "accept_language": "zh-CN"})

    async def test_store_standby_token_keeps_compact_profile_fields(self):
        service = BrowserCaptchaService()
        result = type("Result", (), {})()
        result.token = "standby-profile"
        result.browser_id = 9
        result.browser_epoch = 4
        result.fingerprint = {
            "user_agent": "ua-live",
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
            "device_scale_factor": 3.0,
            "is_mobile": True,
            "has_touch": True,
            "viewport": {"width": 430, "height": 932},
            "big_blob": "drop-me",
        }

        await service._store_standby_token(
            "bucket-profile",
            result,
            project_id="project-a",
            action="IMAGE_GENERATION",
        )

        stored = service._standby_tokens["bucket-profile"][0]
        self.assertEqual(
            stored.fingerprint,
            {
                "user_agent": "ua-live",
                "locale": "en-US",
                "timezone_id": "America/Los_Angeles",
                "device_scale_factor": 3.0,
                "is_mobile": True,
                "has_touch": True,
                "viewport": {"width": 430, "height": 932},
            },
        )

    async def test_refill_retries_until_idle_browser_is_available(self):
        service = BrowserCaptchaService()
        service._browser_count = 2

        with patch(
            "src.services.browser_captcha.config",
            SimpleNamespace(
                browser_standby_refill_idle_seconds=0.01,
                browser_standby_token_pool_enabled=True,
                browser_standby_token_pool_depth=1,
                browser_standby_token_ttl_seconds=45,
            ),
        ):
            with patch.object(
                service,
                "_select_idle_browser_id_for_refill",
                AsyncMock(side_effect=[None, 1]),
            ) as select_mock:
                with patch.object(
                    service,
                    "_acquire_live_token",
                    AsyncMock(
                        return_value=SimpleNamespace(
                            token="filled-token",
                            browser_id=1,
                            browser_ref=1,
                            fingerprint={"user_agent": "ua-fill"},
                            source="live",
                            elapsed_ms=0,
                            browser_epoch=3,
                        )
                    ),
                ) as acquire_mock:
                    await service._refill_standby_token(
                        bucket_key=self.bucket_key,
                        project_id="project-a",
                        action="IMAGE_GENERATION",
                        token_proxy_url=None,
                        preferred_browser_id=0,
                    )

        self.assertGreaterEqual(select_mock.await_count, 2)
        acquire_mock.assert_awaited_once()
        self.assertIn(self.bucket_key, service._standby_tokens)
        self.assertEqual(service._standby_tokens[self.bucket_key][0].token, "filled-token")


if __name__ == "__main__":
    unittest.main()
