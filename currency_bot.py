import telebot
from forex_python.converter import CurrencyRates
import os

# Читаем токен из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

# Инициализация API для конвертации валют
currency_rates = CurrencyRates()

# Список валют для конвертации
currencies = {
    "RUB": "Рубль",
    "UAH": "Гривна",
    "USD": "Доллар",
    "EUR": "Евро",
    "KZT": "Тенге"
}

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Привет! Отправь количество юаней, и я переведу в различные валюты.")

@bot.message_handler(func=lambda message: True)
def convert_currency(message):
    try:
        # Преобразуем введённое сообщение в число
        amount_cny = float(message.text)
        
        # Начинаем формировать ответ
        response = f"Конвертация {amount_cny} юаней (CNY):\n"
        
        # Конвертируем в каждую валюту
        for code, name in currencies.items():
            rate = currency_rates.get_rate("CNY", code)
            converted_amount = round(amount_cny * rate, 2)
            response += f"{converted_amount} {name} ({code})\n"
        
        # Отправляем результат
        bot.send_message(message.chat.id, response)
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите корректное число юаней.")
    except Exception as e:
        bot.send_message(message.chat.id, f"Произошла ошибка: {e}")

bot.polling()
