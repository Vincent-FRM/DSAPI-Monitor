import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dsm", ROOT / "deepseek_usage_monitor.py")
dsm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dsm)


class ProxyRefreshTests(unittest.TestCase):
    def test_http_requests_reload_windows_proxy_settings(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        class FakeOpener:
            def open(self, request, timeout=None):
                return FakeResponse()

        proxy_states = [
            {"https": "http://127.0.0.1:7877"},
            {},
        ]
        handlers = []

        def build_opener(handler):
            handlers.append(handler)
            return FakeOpener()

        with mock.patch.object(dsm.urllib.request, "getproxies",
                               side_effect=proxy_states) as getproxies, \
             mock.patch.object(dsm.urllib.request, "ProxyHandler",
                               side_effect=lambda proxies: dict(proxies)), \
             mock.patch.object(dsm.urllib.request, "build_opener",
                               side_effect=build_opener):
            self.assertEqual(dsm.http_get_json("https://example.test", {}), {"ok": True})
            self.assertEqual(dsm.http_get_json("https://example.test", {}), {"ok": True})

        self.assertEqual(getproxies.call_count, 2)
        self.assertEqual(handlers, proxy_states)


class ParsingTests(unittest.TestCase):
    def test_token_classification_and_sum(self):
        usage = [
            {"type": "REQUEST", "amount": "2"},
            {"type": "PROMPT_CACHE_HIT_TOKEN", "amount": "100"},
            {"type": "PROMPT_CACHE_MISS_TOKEN", "amount": "50"},
            {"type": "RESPONSE_TOKEN", "amount": "25"},
        ]
        got = dsm.sum_usage(usage, "tokens")
        self.assertEqual(got["tokens"], 175)
        self.assertEqual(got["prompt"], 150)
        self.assertEqual(got["output"], 25)
        self.assertEqual(got["requests"], 2)

    def test_by_key_requires_verified_match(self):
        key = "fake-abcd123456789wxyz"
        cost = {"data": [{"currency": "CNY", "series": [{
            "api_key": {"sensitive_id": "fake-ab***wxyz"},
            "buckets": [{"cost": "1.25"}],
        }]}]}
        amount = {"series": [{
            "api_key": {"sensitive_id": "fake-ab***wxyz"},
            "buckets": [{"usage": {"REQUEST": 3, "PROMPT_TOKEN": 20,
                                     "RESPONSE_TOKEN": 5}}],
        }]}
        agg, matched = dsm._bykey_agg(cost, amount, key)
        self.assertTrue(matched)
        self.assertEqual(agg["cost"], 1.25)
        self.assertEqual(agg["tokens"], 25)
        _, unmatched = dsm._bykey_agg(cost, amount, "fake-other000000000zzzz")
        self.assertFalse(unmatched)

    def test_fetch_maps_unmatched_key_to_none(self):
        matched_key = "fake-abcd123456789wxyz"
        missing_key = "fake-other000000000zzzz"
        cost_biz = {"data": [{"currency": "CNY", "series": [{
            "api_key": {"sensitive_id": "fake-ab***wxyz"},
            "buckets": [{"cost": "2"}],
        }]}]}
        amount_biz = {"series": [{
            "api_key": {"sensitive_id": "fake-ab***wxyz"},
            "buckets": [{"usage": {"REQUEST": 1, "PROMPT_TOKEN": 4,
                                     "RESPONSE_TOKEN": 2}}],
        }]}
        wrapped = lambda biz: {"code": 0, "data": {"biz_code": 0, "biz_data": biz}}
        with mock.patch.object(dsm, "platform_get",
                               side_effect=[wrapped(cost_biz), wrapped(amount_biz)]), \
             mock.patch.object(dsm.time, "sleep"):
            got = dsm.fetch_platform_usage("token", [matched_key, missing_key])
        self.assertTrue(got["ok"])
        self.assertIsNotNone(got["by_key"][matched_key])
        self.assertIsNone(got["by_key"][missing_key])


class AggregationTests(unittest.TestCase):
    def _snap(self, name, balance, token_id, cost=1, tokens=10):
        return {
            "name": name,
            "balance": {"currency": "CNY", "topped_up": balance,
                        "granted": 0, "total": balance},
            "today_cost": cost,
            "today_cost_src": "官方",
            "today_date": "2026-08-16",
            "tokens": tokens,
            "detail": {"prompt": 8, "output": 2, "cache_hit": 0, "requests": 1},
            "token_configured": True,
            "token_id": token_id,
            "warnings": [],
        }

    def test_same_platform_account_is_counted_once(self):
        view = dsm.aggregate_view([
            self._snap("key-a", 5, "same"),
            self._snap("key-b", 5, "same"),
        ], 0)
        self.assertEqual(view["money"]["topped_up"]["CNY"], 5)
        self.assertEqual(view["money"]["today"]["CNY"], 1)
        self.assertEqual(view["tokens"], 10)
        self.assertEqual(view["token_coverage"], (1, 1))

    def test_first_failed_balance_does_not_hide_later_success(self):
        bad = self._snap("key-a", 5, "same")
        bad["balance"] = None
        good = self._snap("key-b", 7, "same")
        view = dsm.aggregate_view([bad, good], 0)
        self.assertEqual(view["money"]["topped_up"]["CNY"], 7)


class SecurityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_dpapi_round_trip(self):
        encrypted = dsm.protect_secret("not-a-real-key")
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertNotIn("not-a-real-key", encrypted)
        self.assertEqual(dsm.unprotect_secret(encrypted), "not-a-real-key")

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
    def test_config_never_writes_plaintext_secret(self):
        cfg = {
            "accounts": [{"ds_account": "A", "name": "K",
                          "api_key": "not-a-real-key", "platform_token": "session-secret"}],
            "refresh_seconds": 30, "timezone": "GMT+8", "pin_corner": True,
            "language": "en_US",
        }
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(dsm, "CONFIG_FILE", str(pathlib.Path(td) / "config.json")), \
             mock.patch.object(dsm, "LEGACY_CONFIG_FILE", str(pathlib.Path(td) / "legacy.json")):
            self.assertTrue(dsm.save_config(cfg))
            text = pathlib.Path(dsm.CONFIG_FILE).read_text(encoding="utf-8")
            self.assertNotIn("not-a-real-key", text)
            self.assertNotIn("session-secret", text)
            self.assertIn("dpapi:", text)
            loaded = dsm.load_config()
        self.assertEqual(loaded["accounts"][0]["api_key"], "not-a-real-key")
        self.assertEqual(loaded["language"], "en_US")


class FetchPlanningTests(unittest.TestCase):
    def test_same_token_is_fetched_once(self):
        cfg = {"accounts": [
            {"name": "A", "api_key": "fake-a", "platform_token": "same"},
            {"name": "B", "api_key": "fake-b", "platform_token": "same"},
        ], "timezone": "GMT+8", "language": "en_US"}
        empty = lambda name: {
            "name": name, "warnings": [], "balance": None, "currency": None,
            "fetch_failed": False, "today_date": "2026-08-16",
        }
        app = object.__new__(dsm.MonitorApp)
        td = {"ok": False, "err": "test", "date": "2026-08-16"}
        with mock.patch.object(dsm, "fetch_platform_usage", return_value=td) as fetch, \
             mock.patch.object(app, "_fetch_account",
                               side_effect=lambda acc, name, tz, usage, lang, *args: empty(name)), \
             mock.patch.object(dsm, "prune_state"):
            app._fetch_all(cfg, 0, {})
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], ["fake-a", "fake-b"])
        self.assertEqual(fetch.call_args.args[3], "en_US")
        self.assertEqual(fetch.call_args.args[4], dsm.TIME_TODAY)


class LocalizationTests(unittest.TestCase):
    def test_language_labels_and_fallback(self):
        self.assertEqual(dsm.LANGUAGE_MENU_LABEL, "language")
        self.assertEqual(dsm.LANGUAGE_MENU_OPTIONS,
                         (("中文", "zh_CN"), ("English", "en_US")))
        self.assertEqual(dsm.ALL_LABELS[dsm.LANG_ZH], "账号总量")
        self.assertEqual(dsm.ALL_LABELS[dsm.LANG_EN], "Account Total")
        self.assertEqual(dsm.tr(dsm.LANG_EN, "balance"), "Top-up Balance")
        self.assertEqual(dsm.tr("invalid", "balance"), "充值余额")

    def test_user_token_help_is_localized(self):
        self.assertIn("How to get", dsm.usertoken_help(dsm.LANG_EN))
        self.assertIn("如何获取", dsm.usertoken_help(dsm.LANG_ZH))


class StartupTests(unittest.TestCase):
    def _registry(self):
        registry = mock.MagicMock()
        registry.HKEY_CURRENT_USER = object()
        registry.KEY_READ = 1
        registry.KEY_SET_VALUE = 2
        registry.REG_SZ = 1
        return registry

    def test_startup_is_opt_in_and_matches_current_command(self):
        registry = self._registry()
        registry.QueryValueEx.return_value = (r'"C:\Apps\DSAPI-Monitor.exe"', registry.REG_SZ)
        with mock.patch.object(dsm, "winreg", registry), \
             mock.patch.object(dsm, "startup_command", return_value=r'"C:\Apps\DSAPI-Monitor.exe"'):
            self.assertTrue(dsm.is_startup_enabled())
            registry.QueryValueEx.return_value = (r'"C:\Old\DSAPI-Monitor.exe"', registry.REG_SZ)
            self.assertFalse(dsm.is_startup_enabled())

    def test_startup_setting_writes_and_removes_hkcu_value(self):
        registry = self._registry()
        create_key = mock.MagicMock()
        open_key = mock.MagicMock()
        registry.CreateKey.return_value.__enter__.return_value = create_key
        registry.OpenKey.return_value.__enter__.return_value = open_key
        with mock.patch.object(dsm, "winreg", registry), \
             mock.patch.object(dsm, "startup_command", return_value="current-command"):
            dsm.set_startup_enabled(True)
            registry.SetValueEx.assert_called_once_with(
                create_key, dsm.STARTUP_REG_NAME, 0, registry.REG_SZ, "current-command")
            dsm.set_startup_enabled(False)
            registry.DeleteValue.assert_called_once_with(open_key, dsm.STARTUP_REG_NAME)


class WindowPinTests(unittest.TestCase):
    def test_pinned_window_ignores_press_and_drag(self):
        app = object.__new__(dsm.MonitorApp)
        app._pin_corner = True
        app._drag = (10, 20)
        event = mock.Mock(x_root=100, y_root=200)
        self.assertEqual(app._on_press(event), "break")
        self.assertIsNone(app._drag)
        app._drag = (10, 20)
        self.assertEqual(app._on_drag(event), "break")
        self.assertIsNone(app._drag)


class ModelDimensionTests(unittest.TestCase):
    def test_by_key_models_are_discovered_from_response(self):
        key = "fake-abcd123456789wxyz"
        sid = {"sensitive_id": "fake-ab***wxyz"}
        cost = {"data": [{"currency": "CNY", "series": [
            {"api_key": sid, "model": "future-model-a", "buckets": [{"cost": "1.25"}]},
            {"api_key": sid,
             "buckets": [{"model": {"id": "future-model-b"}, "cost": "2.75"}]},
        ]}]}
        amount = {"series": [
            {"api_key": sid, "model_name": "future-model-a",
             "buckets": [{"usage": {"REQUEST": 2, "PROMPT_TOKEN": 10}}]},
            {"api_key": sid,
             "buckets": [{"model": "future-model-b",
                          "usage": {"REQUEST": 3, "RESPONSE_TOKEN": 5}}]},
        ]}
        got, matched = dsm._bykey_agg(cost, amount, key)
        self.assertTrue(matched)
        self.assertEqual(set(got["by_model"]), {"future-model-a", "future-model-b"})
        self.assertEqual(got["by_model"]["future-model-a"]["cost"], 1.25)
        self.assertEqual(got["by_model"]["future-model-a"]["tokens"], 10)
        self.assertEqual(got["by_model"]["future-model-b"]["cost"], 2.75)
        self.assertEqual(got["by_model"]["future-model-b"]["requests"], 3)

    def test_classic_models_are_discovered_from_day_records(self):
        biz = {"currency": "CNY", "days": [{
            "date": "2026-08-16",
            "data": [
                {"model": "future-model-a", "usage": [
                    {"type": "REQUEST", "amount": 2},
                    {"type": "PROMPT_TOKEN", "amount": 10},
                ]},
                {"model": {"name": "future-model-b"}, "usage": [
                    {"type": "RESPONSE_TOKEN", "amount": 5},
                ]},
            ],
        }]}
        day_map, model_maps, _, currency = dsm.classic_day_maps(biz)
        self.assertEqual(currency, "CNY")
        self.assertIn("2026-08-16", day_map)
        self.assertEqual(set(model_maps), {"future-model-a", "future-model-b"})
        self.assertEqual(
            dsm.aggregate_day_map(model_maps["future-model-a"], "tokens")["requests"], 2)
        self.assertEqual(
            dsm.aggregate_day_map(model_maps["future-model-b"], "tokens")["tokens"], 5)

    def test_model_filter_changes_usage_but_not_balance(self):
        app = object.__new__(dsm.MonitorApp)
        app._lang = dsm.LANG_ZH
        app._selected_model = "future-model-a"
        view = {
            "money": {"topped_up": {"CNY": 100}, "granted": {"CNY": 5},
                      "today": {"CNY": 9}},
            "tokens": 90,
            "detail": {"prompt": 80, "output": 10, "cache_hit": 0,
                       "cache_miss": 0, "requests": 9},
            "model_usage": {"future-model-a": {
                "costs": {"CNY": 3}, "tokens": 30,
                "cost_available": True, "usage_available": True,
                "detail": {"prompt": 25, "output": 5, "cache_hit": 0,
                           "cache_miss": 0, "requests": 3},
            }},
        }
        filtered = app._apply_model_filter(view)
        self.assertEqual(filtered["money"]["today"], {"CNY": 3})
        self.assertEqual(filtered["money"]["topped_up"], {"CNY": 100})
        self.assertEqual(filtered["tokens"], 30)
        self.assertEqual(filtered["detail"]["requests"], 3)

    def test_all_models_is_the_startup_default(self):
        self.assertEqual(dsm.ALL_MODELS_LABELS[dsm.DEFAULT_LANG], "所有模型")

    def test_missing_model_cost_is_not_reported_as_zero(self):
        merged = {}
        dsm.merge_model_usage(merged, {"future-model-a": {
            "cost": 0, "tokens": 10, "prompt": 10, "output": 0,
            "cache_hit": 0, "cache_miss": 0, "requests": 1,
            "currency": "CNY", "cost_available": False, "usage_available": True,
        }})
        self.assertFalse(merged["future-model-a"]["cost_available"])
        self.assertEqual(merged["future-model-a"]["costs"], {})
        self.assertEqual(merged["future-model-a"]["tokens"], 10)


class TimeDimensionTests(unittest.TestCase):
    def test_six_dimensions_and_calendar_boundaries(self):
        fixed = dsm.datetime(2026, 3, 1, 12, tzinfo=dsm.CN_TZ)
        self.assertEqual(len(dsm.TIME_DIMENSIONS), 6)
        self.assertEqual(dsm.DEFAULT_TIME_DIMENSION, dsm.TIME_TODAY)
        self.assertEqual(dsm.usage_date_range(dsm.TIME_TODAY, dsm.CN_TZ, fixed),
                         (dsm.date(2026, 3, 1), dsm.date(2026, 3, 1)))
        self.assertEqual(dsm.usage_date_range(dsm.TIME_YESTERDAY, dsm.CN_TZ, fixed),
                         (dsm.date(2026, 2, 28), dsm.date(2026, 2, 28)))
        self.assertEqual(dsm.usage_date_range(dsm.TIME_LAST_7_DAYS, dsm.CN_TZ, fixed),
                         (dsm.date(2026, 2, 23), dsm.date(2026, 3, 1)))
        self.assertEqual(dsm.usage_date_range(dsm.TIME_LAST_30_DAYS, dsm.CN_TZ, fixed),
                         (dsm.date(2026, 1, 31), dsm.date(2026, 3, 1)))
        self.assertEqual(dsm.usage_date_range(dsm.TIME_THIS_MONTH, dsm.CN_TZ, fixed),
                         (dsm.date(2026, 3, 1), dsm.date(2026, 3, 1)))
        self.assertEqual(dsm.usage_date_range(dsm.TIME_LAST_MONTH, dsm.CN_TZ, fixed),
                         (dsm.date(2026, 2, 1), dsm.date(2026, 2, 28)))

    def test_by_key_query_uses_selected_range(self):
        fixed = dsm.datetime(2026, 8, 16, 12, tzinfo=dsm.CN_TZ)
        cost_biz = {"data": [{"currency": "CNY", "series": [{
            "api_key": {"sensitive_id": "fake-ab***wxyz"},
            "buckets": [{"cost": "3.5"}],
        }]}]}
        amount_biz = {"series": [{
            "api_key": {"sensitive_id": "fake-ab***wxyz"},
            "buckets": [{"usage": {"REQUEST": 4, "PROMPT_TOKEN": 20,
                                     "RESPONSE_TOKEN": 5}}],
        }]}
        wrapped = lambda biz: {"code": 0, "data": {"biz_code": 0, "biz_data": biz}}
        with mock.patch.object(dsm, "platform_get",
                               side_effect=[wrapped(cost_biz), wrapped(amount_biz)]) as get, \
             mock.patch.object(dsm.time, "sleep"):
            got = dsm.fetch_platform_usage(
                "token", ["fake-abcd123456789wxyz"], dsm.CN_TZ, dsm.LANG_ZH,
                dsm.TIME_LAST_7_DAYS, fixed)
        self.assertTrue(got["ok"])
        self.assertEqual((got["range_start"], got["range_end"]),
                         ("2026-08-10", "2026-08-16"))
        expected_start = int(dsm.datetime(2026, 8, 10, tzinfo=dsm.CN_TZ).timestamp())
        expected_end = int(dsm.datetime(2026, 8, 17, tzinfo=dsm.CN_TZ).timestamp())
        self.assertEqual(get.call_args_list[0].args[2]["start"], expected_start)
        self.assertEqual(get.call_args_list[0].args[2]["end"], expected_end)
        self.assertEqual(got["cost"], 3.5)
        self.assertEqual(got["tokens"], 25)
        self.assertEqual(got["requests"], 4)

    def test_classic_fallback_combines_cross_month_range(self):
        fixed = dsm.datetime(2026, 2, 3, 12, tzinfo=dsm.CN_TZ)

        def classic(days, currency=None):
            data = {"days": days}
            if currency:
                data["currency"] = currency
            return data

        def day(value, typ, amount):
            return {"date": value, "data": [{"type": typ, "amount": amount}]}

        wrapped = lambda biz: {"code": 0, "data": {"biz_code": 0, "biz_data": biz}}
        responses = [
            {}, {},
            wrapped(classic([day("2026-01-01", "REQUEST", 99),
                             day("2026-01-30", "REQUEST", 2),
                             day("2026-01-30", "PROMPT_TOKEN", 10)])),
            wrapped(classic([day("2026-01-01", "COST", 99),
                             day("2026-01-30", "COST", 1.25)], "CNY")),
            wrapped(classic([day("2026-02-03", "REQUEST", 3),
                             day("2026-02-03", "RESPONSE_TOKEN", 5)])),
            wrapped(classic([day("2026-02-03", "COST", 2.75)], "CNY")),
        ]
        with mock.patch.object(dsm, "platform_get", side_effect=responses) as get, \
             mock.patch.object(dsm.time, "sleep"):
            got = dsm.fetch_platform_usage(
                "token", [], dsm.CN_TZ, dsm.LANG_ZH,
                dsm.TIME_LAST_7_DAYS, fixed)
        self.assertTrue(got["ok"])
        self.assertEqual(got["cost"], 4.0)
        self.assertEqual(got["tokens"], 15)
        self.assertEqual(got["requests"], 5)
        classic_params = [call.args[2] for call in get.call_args_list[2:]]
        self.assertEqual(classic_params, [
            {"month": 1, "year": 2026}, {"month": 1, "year": 2026},
            {"month": 2, "year": 2026}, {"month": 2, "year": 2026},
        ])

    def test_time_dimension_labels_are_localized(self):
        self.assertEqual(dsm.time_dimension_label(dsm.LANG_ZH, dsm.TIME_LAST_30_DAYS),
                         "近 30 天")
        self.assertEqual(dsm.time_dimension_label(dsm.LANG_EN, dsm.TIME_LAST_MONTH),
                         "Last Month")


class IconTests(unittest.TestCase):
    @unittest.skipUnless(dsm.HAS_TRAY, "Pillow is not installed")
    def test_black_blue_ds_icon_asset_and_tray_resize(self):
        source_path = ROOT / "app_icon_source_v2.png"
        self.assertTrue(source_path.is_file())
        icon = dsm.make_app_image(32)
        self.assertEqual(icon.size, (32, 32))
        pixels = list(icon.convert("RGB").get_flattened_data())
        self.assertTrue(any(b > r + 30 and b > g for r, g, b in pixels))
        self.assertTrue(any(max(r, g, b) < 10 for r, g, b in pixels))


if __name__ == "__main__":
    unittest.main()
