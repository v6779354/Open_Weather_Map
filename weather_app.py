"""Текущая погода по городу или координатам через OpenWeather."""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("API_KEY")

BASE_URL = "https://api.openweathermap.org"
REQUEST_TIMEOUT = 10
RETRY_DELAYS = (1, 2, 4)
CACHE_PATH = Path(__file__).resolve().with_name("weather_cache.json")
CACHE_MAX_AGE = timedelta(hours=3)


class WeatherError(Exception):
    """Ошибка получения погоды с понятным пользователю сообщением."""


class WeatherUnavailableError(WeatherError):
    """Временная недоступность API после исчерпания повторов."""


def _request(endpoint: str, params: dict):
    if not API_KEY or not API_KEY.strip() or API_KEY.strip() == "...":
        raise WeatherError("Укажите ключ OpenWeather в файле .env: API_KEY=ваш_ключ.")

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = requests.get(
                f"{BASE_URL}{endpoint}",
                params={**params, "appid": API_KEY.strip()},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.Timeout:
            message = "OpenWeather не ответил вовремя. Попробуйте позже."
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            message = "Не удалось связаться с OpenWeather. Проверьте подключение к интернету."
        except requests.RequestException:
            raise WeatherError("Не удалось выполнить запрос к OpenWeather.") from None
        else:
            if response.status_code == 429:
                message = "Превышен лимит запросов OpenWeather. Попробуйте позже."
            elif response.status_code in (500, 502, 503, 504):
                message = f"OpenWeather временно недоступен (HTTP {response.status_code})."
            else:
                break

        if attempt == len(RETRY_DELAYS):
            raise WeatherUnavailableError(message) from None
        delay = RETRY_DELAYS[attempt]
        print(f"{message} Повтор {attempt + 1}/3 через {delay} с.")
        time.sleep(delay)

    if response.status_code == 401:
        raise WeatherError("Ключ OpenWeather недействителен или ещё не активирован. Проверьте API_KEY в .env.")
    if response.status_code != 200:
        raise WeatherError(f"OpenWeather вернул ошибку HTTP {response.status_code}. Попробуйте позже.")

    try:
        return response.json()
    except ValueError:
        raise WeatherError("OpenWeather вернул некорректный ответ. Попробуйте позже.") from None


def get_coordinates(city: str) -> tuple[float, float]:
    """Вернуть координаты города; при ошибке вызвать WeatherError."""
    city = city.strip()
    if not city:
        raise WeatherError("Название города не должно быть пустым.")

    data = _request("/geo/1.0/direct", {"q": city, "limit": 1, "lang": "ru"})
    if not isinstance(data, list):
        raise WeatherError("OpenWeather вернул некорректный ответ геокодинга.")
    if not data:
        raise WeatherError(f"Город «{city}» не найден. Проверьте название.")

    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, TypeError, ValueError):
        raise WeatherError("В ответе OpenWeather отсутствуют корректные координаты.") from None


def get_weather_by_coordinates(lat: float, lon: float) -> dict:
    """Вернуть данные текущей погоды; при ошибке вызвать WeatherError."""
    validate_coordinates(lat, lon)
    data = _request(
        "/data/2.5/weather",
        {"lat": lat, "lon": lon, "units": "metric", "lang": "ru"},
    )
    validate_weather(data)
    return data


def validate_weather(data: dict) -> None:
    try:
        temperature = data["main"]["temp"]
        description = data["weather"][0]["description"]
        if (
            not isinstance(temperature, (int, float))
            or isinstance(temperature, bool)
            or not math.isfinite(temperature)
            or not isinstance(description, str)
            or not description.strip()
        ):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise WeatherError("В ответе OpenWeather отсутствуют корректные данные о погоде.") from None


def validate_coordinates(lat: float, lon: float) -> None:
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and -limit <= value <= limit
        for value, limit in ((lat, 90), (lon, 180))
    ):
        raise WeatherError("Широта должна быть от −90 до 90, долгота — от −180 до 180.")


def save_cache(city: str, lat: float, lon: float, weather: dict) -> None:
    cache = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "weather": weather,
    }
    temporary_path = CACHE_PATH.with_suffix(".json.tmp")
    try:
        temporary_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary_path.replace(CACHE_PATH)
    except (OSError, ValueError, TypeError):
        print("Предупреждение: не удалось сохранить погоду в кэш.")


def load_cache() -> dict | None:
    """Вернуть только корректный кэш возрастом строго меньше трёх часов."""
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
        age = datetime.now(timezone.utc) - fetched_at
        if not timedelta(0) <= age < CACHE_MAX_AGE:
            return None
        if not isinstance(cache["city"], str):
            return None
        validate_coordinates(cache["lat"], cache["lon"])
        validate_weather(cache["weather"])
        return cache
    except (OSError, ValueError, TypeError, KeyError, OverflowError, WeatherError):
        return None


def print_weather(city: str, lat: float, lon: float, weather: dict) -> None:
    location = f"{city} ({lat}, {lon})" if city else f"({lat}, {lon})"
    print(f"Погода для {location}: {weather['main']['temp']}°C, {weather['weather'][0]['description']}")


def offer_cache() -> None:
    cache = load_cache()
    if cache is None:
        print("Свежего кэша нет (нужны данные младше 3 часов).")
        return
    location = cache["city"] or "без названия города"
    print(
        f"Последний успешный запрос: {location}, {cache['lat']}, {cache['lon']}; "
        f"получен {cache['fetched_at']}."
    )
    if input("Показать эти данные из кэша? [д/Н]: ").strip().lower() in ("д", "да", "y", "yes"):
        print("Данные из кэша:")
        print_weather(cache["city"], cache["lat"], cache["lon"], cache["weather"])


def main() -> int:
    try:
        while True:
            print("\n1 — по городу\n2 — по координатам\n0 — выход")
            mode = input("Выберите режим: ").strip()
            if mode == "0":
                return 0
            if mode not in ("1", "2"):
                print("Введите 1, 2 или 0.")
                continue
            try:
                if mode == "1":
                    city = input("Введите город: ").strip()
                    lat, lon = get_coordinates(city)
                else:
                    city = ""
                    try:
                        lat = float(input("Введите широту (−90…90): ").strip().replace(",", "."))
                        lon = float(input("Введите долготу (−180…180): ").strip().replace(",", "."))
                    except ValueError:
                        raise WeatherError("Координаты должны быть числами.") from None
                weather = get_weather_by_coordinates(lat, lon)
                if not city:
                    name = weather.get("name")
                    city = name.strip() if isinstance(name, str) else ""
                save_cache(city, lat, lon, weather)
                print_weather(city, lat, lon, weather)
            except WeatherUnavailableError as error:
                print(f"Ошибка: {error}")
                offer_cache()
            except WeatherError as error:
                print(f"Ошибка: {error}")
    except (EOFError, KeyboardInterrupt):
        print("\nВвод отменён.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
