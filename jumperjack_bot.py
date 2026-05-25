"""
Бот-автокликер для игры LumberJack (@gamebot) в Telegram Desktop.

Принцип работы:
  1. Делает скриншот заданной области экрана (окна игры).
  2. Определяет, с какой стороны от ствола стоит дровосек.
  3. Смотрит, есть ли ветка над головой дровосека (на той же стороне ствола,
     на ближайшем «следующем» уровне).
  4. Если ветка над головой — нажимает стрелку в противоположную сторону
     (дровосек перепрыгивает). Иначе — нажимает стрелку в свою сторону
     (рубит дерево).

Зависимости (см. requirements.txt):
  - mss          — быстрый захват экрана
  - Pillow       — работа с изображением
  - numpy        — анализ пикселей
  - pynput       — эмуляция нажатий клавиш

Запуск:
  1. Откройте Telegram Desktop с игрой LumberJack так, чтобы окно игры было
     полностью видно и не перекрывалось.
  2. Запустите калибровку:        python jumperjack_bot.py --calibrate
     Появится окно с инструкцией: наведите курсор последовательно на
     левый-верхний и правый-нижний углы игровой области, нажмите Enter.
     Координаты сохранятся в jumperjack_region.json.
  3. Запустите бота:              python jumperjack_bot.py
     Окно игры должно быть в фокусе. Остановка — Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

import numpy as np
from mss import mss
from PIL import Image
from pynput.keyboard import Controller, Key

REGION_FILE = os.path.join(os.path.dirname(__file__), "jumperjack_region.json")

# Цвет ствола дерева на скриншоте — коричневый.
# Подобрано по картинке: r≈140-180, g≈90-120, b≈50-80.
TRUNK_RGB = (165, 105, 60)
TRUNK_TOLERANCE = 50

# Цвет ветки (зелёная листва) — насыщенный зелёный.
BRANCH_RGB = (140, 200, 110)
BRANCH_TOLERANCE = 70

# Цвет рубашки дровосека — красный.
JACK_RGB = (210, 60, 50)
JACK_TOLERANCE = 70

# Сколько раз в секунду делать ход.
TICKS_PER_SECOND = 6


@dataclass
class Region:
    left: int
    top: int
    width: int
    height: int

    @classmethod
    def load(cls) -> "Region":
        if not os.path.exists(REGION_FILE):
            raise SystemExit(
                f"Не найден {REGION_FILE}. Запустите: python jumperjack_bot.py --calibrate"
            )
        with open(REGION_FILE) as f:
            data = json.load(f)
        return cls(**data)

    def save(self) -> None:
        with open(REGION_FILE, "w") as f:
            json.dump(self.__dict__, f, indent=2)


def color_mask(img: np.ndarray, target: tuple[int, int, int], tol: int) -> np.ndarray:
    """Возвращает булеву маску пикселей, близких к target в пределах tol."""
    diff = np.abs(img.astype(np.int16) - np.array(target, dtype=np.int16))
    return np.all(diff <= tol, axis=-1)


def find_jack_side(img: np.ndarray, trunk_x: int) -> str:
    """Определить, стоит ли дровосек слева или справа от ствола.

    Ищем красные пиксели рубашки в нижней трети картинки и сравниваем
    среднюю x-координату со стволом.
    """
    h, w = img.shape[:2]
    bottom = img[int(h * 0.55) : int(h * 0.95), :]
    mask = color_mask(bottom, JACK_RGB, JACK_TOLERANCE)
    ys, xs = np.where(mask)
    if len(xs) < 30:
        # Не нашли — по умолчанию считаем, что слева (как на стартовой картинке).
        return "left"
    mean_x = float(xs.mean())
    return "left" if mean_x < trunk_x else "right"


def find_trunk_x(img: np.ndarray) -> int:
    """Найти горизонтальную координату центра ствола."""
    h, w = img.shape[:2]
    # Берём средний горизонтальный срез, ствол всегда тянется вертикально.
    middle = img[int(h * 0.15) : int(h * 0.55), :]
    mask = color_mask(middle, TRUNK_RGB, TRUNK_TOLERANCE)
    xs = np.where(mask.any(axis=0))[0]
    if len(xs) == 0:
        return w // 2
    # Берём моду: x, где больше всего вертикальных пикселей ствола.
    col_counts = mask.sum(axis=0)
    return int(np.argmax(col_counts))


def branch_above_jack(img: np.ndarray, trunk_x: int, jack_side: str) -> bool:
    """Проверить, есть ли ветка над головой дровосека.

    «Над головой» = на той же стороне от ствола, на уровне нижней четверти
    верхней половины картинки (то есть прямо над дровосеком).
    """
    h, w = img.shape[:2]
    # Полоса по вертикали — там, где будет «следующая» ветка над дровосеком.
    y1, y2 = int(h * 0.30), int(h * 0.55)
    if jack_side == "left":
        x1, x2 = max(0, trunk_x - int(w * 0.35)), trunk_x - 5
    else:
        x1, x2 = trunk_x + 5, min(w, trunk_x + int(w * 0.35))
    if x2 <= x1 or y2 <= y1:
        return False
    roi = img[y1:y2, x1:x2]
    mask = color_mask(roi, BRANCH_RGB, BRANCH_TOLERANCE)
    # Считаем веткой, если зелёных пикселей больше порога.
    return int(mask.sum()) > 200


def grab(sct, region: Region) -> np.ndarray:
    raw = sct.grab(
        {
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
        }
    )
    img = Image.frombytes("RGB", raw.size, raw.rgb)
    return np.asarray(img)


def run(region: Region) -> None:
    keyboard = Controller()
    period = 1.0 / TICKS_PER_SECOND
    print(f"Старт. Регион: {region}. Ctrl+C — стоп.")
    with mss() as sct:
        while True:
            t0 = time.monotonic()
            img = grab(sct, region)
            trunk_x = find_trunk_x(img)
            jack_side = find_jack_side(img, trunk_x)
            danger = branch_above_jack(img, trunk_x, jack_side)

            if danger:
                # Над головой ветка — прыгаем в противоположную сторону.
                key = Key.right if jack_side == "left" else Key.left
                action = "ПРЫЖОК"
            else:
                # Веток нет — рубим, нажимая стрелку в свою сторону.
                key = Key.left if jack_side == "left" else Key.right
                action = "рубим"

            keyboard.press(key)
            keyboard.release(key)
            print(
                f"side={jack_side:<5} branch={'Y' if danger else 'N'} -> {action} ({key})"
            )

            dt = time.monotonic() - t0
            if dt < period:
                time.sleep(period - dt)


def calibrate() -> None:
    """Простая текстовая калибровка через ввод координат вручную."""
    print(
        "Калибровка. Откройте окно игры. Понадобятся координаты\n"
        "левого-верхнего и правого-нижнего угла игровой области.\n"
        "Подсказка: можно подвести курсор и посмотреть координаты\n"
        "в любом screenshot-инструменте, либо воспользоваться `xdotool getmouselocation`\n"
        "на Linux или встроенными средствами ОС.\n"
    )
    try:
        left = int(input("left (x верхнего-левого угла): ").strip())
        top = int(input("top  (y верхнего-левого угла): ").strip())
        right = int(input("right (x нижнего-правого угла): ").strip())
        bottom = int(input("bottom (y нижнего-правого угла): ").strip())
    except ValueError:
        raise SystemExit("Координаты должны быть целыми числами.")
    if right <= left or bottom <= top:
        raise SystemExit("Правый-нижний угол должен быть правее и ниже левого-верхнего.")
    region = Region(left=left, top=top, width=right - left, height=bottom - top)
    region.save()
    print(f"Сохранено в {REGION_FILE}: {region}")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="LumberJack auto-player")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Задать координаты игровой области и сохранить их.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Сколько секунд ждать перед стартом (чтобы успеть переключиться в окно игры).",
    )
    args = parser.parse_args(argv)

    if args.calibrate:
        calibrate()
        return

    region = Region.load()
    if args.delay > 0:
        print(f"Старт через {args.delay} c — переключитесь в окно игры.")
        time.sleep(args.delay)
    try:
        run(region)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")


if __name__ == "__main__":
    main(sys.argv[1:])
