"""
Бот-автоигрок для игры LumberJack в Telegram (@gamebot).

Идея:
  - Игра запущена в окне Telegram Desktop (или в браузере).
  - Скрипт раз в N миллисекунд делает скриншот заданной области экрана —
    зоны с деревом игры.
  - По цветам определяет, есть ли ветка слева или справа от ствола
    на уровне головы дровосека.
  - Дровосек по умолчанию стоит с одной стороны и рубит (нажимаем ту же
    стрелку каждый такт). Если над головой появилась ветка с той же
    стороны — нажимаем противоположную стрелку, чтобы перейти на другую
    сторону. После этого продолжаем рубить с новой стороны.

Использование:
  1. Установить зависимости: pip install -r requirements.txt
  2. Открыть LumberJack в Telegram Desktop, развернуть окно игры так,
     чтобы оно было хорошо видно и не перекрывалось.
  3. Запустить:
        python jumberjack_bot.py --calibrate
     — наведите мышь сначала на ВЕРХНИЙ-ЛЕВЫЙ угол игрового поля
     (где небо рядом с верхушкой дерева), нажмите Enter, затем на
     НИЖНИЙ-ПРАВЫЙ угол (на уровне ног дровосека) и снова Enter.
     Скрипт сохранит координаты в jumberjack_region.json.
  4. Запустить бота:
        python jumberjack_bot.py
     Окно с игрой должно быть в фокусе. У вас будет 3 секунды, чтобы
     переключиться на него. Для остановки — Ctrl+C в терминале.

Флаги:
  --side {left,right}  — с какой стороны бот начинает рубить
                         (по умолчанию left).
  --delay <seconds>    — пауза между нажатиями (по умолчанию 0.06).
  --debug              — сохранять отладочные скриншоты в /tmp.
"""

import argparse
import json
import os
import sys
import time

try:
    import mss
    import numpy as np
    from pynput.keyboard import Controller, Key
except ImportError as e:
    print(
        "Не хватает зависимостей: " + str(e) + "\n"
        "Запустите: pip install mss pynput numpy"
    )
    sys.exit(1)


REGION_FILE = "jumberjack_region.json"


def calibrate():
    """Простая ручная калибровка через позицию курсора мыши."""
    try:
        from pynput.mouse import Controller as MouseController
    except ImportError:
        print("Нужен pynput. pip install pynput")
        sys.exit(1)

    mouse = MouseController()
    input("Наведите курсор на ВЕРХНИЙ-ЛЕВЫЙ угол игрового поля и нажмите Enter...")
    x1, y1 = mouse.position
    input("Теперь на НИЖНИЙ-ПРАВЫЙ угол (на уровне ног дровосека) и Enter...")
    x2, y2 = mouse.position
    region = {
        "left": int(min(x1, x2)),
        "top": int(min(y1, y2)),
        "width": int(abs(x2 - x1)),
        "height": int(abs(y2 - y1)),
    }
    with open(REGION_FILE, "w") as f:
        json.dump(region, f, indent=2)
    print("Сохранено в", REGION_FILE, ":", region)


def load_region():
    if not os.path.exists(REGION_FILE):
        print(
            "Регион игры не задан. Запустите: python jumberjack_bot.py --calibrate"
        )
        sys.exit(1)
    with open(REGION_FILE) as f:
        return json.load(f)


def is_sky(pixels):
    """Маска неба: светло-голубой/белёсый цвет."""
    b, g, r = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    return (b > 180) & (g > 180) & (r > 170) & (b >= r)


def is_branch_or_trunk(pixels):
    """Маска ветки или ствола: тёмные/коричнево-зелёные пиксели."""
    return ~is_sky(pixels)


def detect_branch(frame):
    """
    На вход — BGR-изображение игровой области.
    Возвращает 'left' / 'right' / None — с какой стороны ствола ветка
    на уровне головы дровосека (верхняя четверть кадра).

    Логика:
      - Берём горизонтальную полосу примерно над головой дровосека
        (верхняя ~25% кадра, начиная с ~5% сверху, чтобы пропустить
        счётчик очков).
      - Делим её на левую и правую половины относительно центра ствола.
      - Ствол всегда по центру, ветки выпирают за пределы ствола в
        стороны. Если в боковой зоне много "не-неба" пикселей —
        там ветка.
    """
    h, w, _ = frame.shape

    band_top = int(h * 0.18)
    band_bottom = int(h * 0.40)
    band = frame[band_top:band_bottom]

    center_x = w // 2
    trunk_half = int(w * 0.10)  # ствол занимает ~20% ширины кадра

    left_zone = band[:, : center_x - trunk_half]
    right_zone = band[:, center_x + trunk_half :]

    left_fill = is_branch_or_trunk(left_zone).mean() if left_zone.size else 0.0
    right_fill = is_branch_or_trunk(right_zone).mean() if right_zone.size else 0.0

    threshold = 0.12  # подобрано эмпирически
    left_has = left_fill > threshold
    right_has = right_fill > threshold

    if left_has and not right_has:
        return "left"
    if right_has and not left_has:
        return "right"
    if left_has and right_has:
        # если ветки с обеих сторон — выбираем ту, где больше "массы"
        return "left" if left_fill >= right_fill else "right"
    return None


def play(side, delay, debug=False):
    region = load_region()
    keyboard = Controller()
    key_for = {"left": Key.left, "right": Key.right}

    print("Стартую через 3 секунды. Переключитесь на окно игры.")
    time.sleep(3)
    print(f"Поехали. Начальная сторона: {side}. Ctrl+C для остановки.")

    tick = 0
    with mss.mss() as sct:
        try:
            while True:
                raw = np.array(sct.grab(region))
                frame = raw[:, :, :3]  # BGRA -> BGR

                branch = detect_branch(frame)

                if branch == side:
                    # ветка с нашей стороны — переходим на другую
                    side = "right" if side == "left" else "left"

                keyboard.press(key_for[side])
                keyboard.release(key_for[side])

                if debug and tick % 20 == 0:
                    try:
                        from PIL import Image

                        Image.fromarray(frame[:, :, ::-1]).save(
                            f"/tmp/jumberjack_{tick:05d}.png"
                        )
                    except Exception:
                        pass

                tick += 1
                time.sleep(delay)
        except KeyboardInterrupt:
            print("\nОстановлено.")


def main():
    p = argparse.ArgumentParser(description="Auto-player for Telegram LumberJack")
    p.add_argument("--calibrate", action="store_true", help="Задать регион игры")
    p.add_argument(
        "--side", choices=["left", "right"], default="left",
        help="С какой стороны дровосек стоит в начале (по умолчанию left)",
    )
    p.add_argument(
        "--delay", type=float, default=0.06,
        help="Пауза между нажатиями, сек (по умолчанию 0.06)",
    )
    p.add_argument("--debug", action="store_true", help="Сохранять кадры в /tmp")
    args = p.parse_args()

    if args.calibrate:
        calibrate()
        return

    play(args.side, args.delay, args.debug)


if __name__ == "__main__":
    main()
