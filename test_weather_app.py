"""Проверки без сети, настоящего API-ключа и задержек."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import weather_app as app


WEATHER = {"main": {"temp": 12.5}, "weather": [{"description": "ясно"}], "name": "Москва"}


def response(status=200, data=None):
    return Mock(status_code=status, json=Mock(return_value=WEATHER if data is None else data))


class WeatherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache_path = Path(directory.name) / "weather_cache.json"
        for patcher in (patch.object(app, "CACHE_PATH", self.cache_path), patch.object(app, "API_KEY", "test-key")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        output = redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def run_menu(self, inputs):
        with patch("builtins.input", side_effect=inputs):
            self.assertEqual(app.main(), 0)

    def test_city_mode_caches_success(self):
        with patch.object(app.requests, "get", side_effect=[response(data=[{"lat": 55.75, "lon": 37.62}]), response()]) as get:
            self.run_menu(["1", " Москва ", "0"])
        self.assertEqual(get.call_count, 2)
        cache = app.load_cache()
        self.assertEqual(cache["city"], "Москва")
        self.assertEqual((cache["lat"], cache["lon"]), (55.75, 37.62))
        self.assertEqual(cache["weather"], WEATHER)
        self.assertIsNotNone(datetime.fromisoformat(cache["fetched_at"]).tzinfo)
        self.assertIn("12.5°C, ясно", self.output.getvalue())

    def test_coordinate_mode_skips_geocoding(self):
        with patch.object(app.requests, "get", return_value=response()) as get:
            self.run_menu(["2", "55,75", "37.62", "0"])
        get.assert_called_once()
        self.assertTrue(get.call_args.args[0].endswith("/data/2.5/weather"))
        self.assertEqual(get.call_args.kwargs["params"]["lat"], 55.75)
        self.assertEqual(app.load_cache()["city"], "Москва")

    def test_invalid_input_does_not_request_api(self):
        with patch.object(app.requests, "get") as get:
            self.run_menu(["9", "1", " ", "2", "abc", "2", "91", "0", "2", "nan", "0", "0"])
        get.assert_not_called()
        self.assertFalse(self.cache_path.exists())

    def test_coordinate_limits(self):
        for lat, lon in ((-90, -180), (90, 180), (0, 0)):
            app.validate_coordinates(lat, lon)
        for lat, lon in ((91, 0), (0, -181), (float("inf"), 0), (0, float("nan"))):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(app.WeatherError):
                app.validate_coordinates(lat, lon)

    def test_retryable_errors_exhaust_three_retries(self):
        for failure in (response(429), response(503), requests.Timeout(), requests.ConnectionError()):
            with self.subTest(failure=failure):
                with patch.object(app.requests, "get", side_effect=[failure] * 4) as get, patch.object(app.time, "sleep") as sleep:
                    with self.assertRaises(app.WeatherUnavailableError):
                        app.get_weather_by_coordinates(55, 37)
                self.assertEqual(get.call_count, 4)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])

    def test_success_stops_retries(self):
        with patch.object(app.requests, "get", side_effect=[requests.Timeout(), response(429), response()]) as get, patch.object(app.time, "sleep") as sleep:
            self.assertEqual(app.get_weather_by_coordinates(55, 37), WEATHER)
        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_permanent_errors_are_not_retried(self):
        for status in (400, 401, 404):
            with self.subTest(status=status), patch.object(app.requests, "get", return_value=response(status)) as get, patch.object(app.time, "sleep") as sleep:
                with self.assertRaises(app.WeatherError) as error:
                    app.get_weather_by_coordinates(55, 37)
                self.assertNotIsInstance(error.exception, app.WeatherUnavailableError)
                get.assert_called_once()
                sleep.assert_not_called()

    def test_cache_age_and_corruption(self):
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        app.save_cache("Москва", 55, 37, WEATHER)
        cache = json.loads(self.cache_path.read_text())
        for age, valid in ((timedelta(hours=2, minutes=59), True), (timedelta(hours=3), False), (timedelta(hours=4), False), (timedelta(seconds=-1), False)):
            cache["fetched_at"] = (now - age).isoformat()
            self.cache_path.write_text(json.dumps(cache))
            with self.subTest(age=age), patch.object(app, "datetime", wraps=datetime) as clock:
                clock.now.return_value = now
                self.assertEqual(app.load_cache() is not None, valid)
        for contents in ("broken", "null", "[]", "{}", '{"fetched_at": "invalid"}'):
            self.cache_path.write_text(contents)
            self.assertIsNone(app.load_cache())
        self.cache_path.unlink()
        self.assertIsNone(app.load_cache())

    def test_network_failure_offers_original_cache_and_preserves_it(self):
        app.save_cache("Москва", 55, 37, WEATHER)
        original = self.cache_path.read_bytes()
        for choice in ("да", "нет"):
            self.output.truncate(0)
            self.output.seek(0)
            with patch.object(app.requests, "get", side_effect=requests.ConnectionError()), patch.object(app.time, "sleep"):
                self.run_menu(["1", "London", choice, "0"])
            self.assertIn("Последний успешный запрос: Москва", self.output.getvalue())
            self.assertEqual("Данные из кэша:" in self.output.getvalue(), choice == "да")
            self.assertEqual(self.cache_path.read_bytes(), original)

    def test_missing_cache_does_not_ask_for_confirmation(self):
        with patch.object(app.requests, "get", side_effect=requests.Timeout()), patch.object(app.time, "sleep"):
            self.run_menu(["2", "55", "37", "0"])
        self.assertIn("Свежего кэша нет", self.output.getvalue())

    def test_invalid_weather_does_not_overwrite_cache(self):
        app.save_cache("Москва", 55, 37, WEATHER)
        original = self.cache_path.read_bytes()
        with patch.object(app.requests, "get", return_value=response(data={})):
            self.run_menu(["2", "55", "37", "0"])
        self.assertEqual(self.cache_path.read_bytes(), original)

    def test_write_failure_does_not_hide_success(self):
        with patch.object(app.requests, "get", return_value=response()), patch.object(Path, "write_text", side_effect=OSError):
            self.run_menu(["2", "55", "37", "0"])
        self.assertIn("не удалось сохранить", self.output.getvalue())
        self.assertIn("12.5°C, ясно", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
