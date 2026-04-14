import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ==================== КОНСТАНТЫ И НАСТРОЙКИ ====================

# Берем токен из поля "Bot Token" в панели BotHost
BOT_TOKEN = os.getenv("BOT_TOKEN") 
# Берем ID чата из поля "Переменные окружения", если нет — используем стандартный
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "-1003828753369"))

# Настройки столов (11 столов с категориями, ценами и видео)
# Если видео лежат в папке, укажите путь, например: "videos/standart.mp4"
TABLES = {
    "1": {"name": "Стол 1", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "2": {"name": "Стол 2", "category": "Комфорт", "price": 2000, "video": "komfort.mp4", "available": True},
    "3": {"name": "Стол 3", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "4": {"name": "Стол 4", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "5": {"name": "Стол 5", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "6": {"name": "Стол 6", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "7": {"name": "Стол 7", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "8": {"name": "Стол 8", "category": "Стандарт", "price": 500, "video": "standart.mp4", "available": True},
    "9": {"name": "Стол 9", "category": "Комфорт +", "price": 2000, "video": "komfortplus.mp4", "available": True},
    "10": {"name": "Стол 10", "category": "Комфорт +", "price": 2000, "video": "komfortplus.mp4", "available": True},
    "11": {"name": "Стол 11", "category": "Бизнес", "price": 0, "video": "biznes.mp4", "available": False}, # Недоступен для брони
}

router = Router()

# ==================== БАЗА ДАННЫХ (SQLite) ====================
DB_NAME = "club.db"

class Database:
    @staticmethod
    async def init_db():
        """Создает таблицу бронирований при запуске, если её нет."""
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    target_date TEXT,
                    table_type TEXT,
                    status TEXT
                )
            ''')
            await db.commit()

    @staticmethod
    async def add_booking(user_id: int, username: str, target_date: str, table_type: str) -> int:
        """Добавляет заявку со статусом 'pending' и возвращает её ID."""
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "INSERT INTO bookings (user_id, username, target_date, table_type, status) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, target_date, table_type, "pending")
            )
            await db.commit()
            return cursor.lastrowid

    @staticmethod
    async def update_status(booking_id: int, status: str):
        """Обновляет статус бронирования ('confirmed', 'rejected' или 'cancelled')."""
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
            await db.commit()

    @staticmethod
    async def get_booking(booking_id: int) -> dict:
        """Получает данные о бронировании по ID."""
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("SELECT user_id, target_date, table_type FROM bookings WHERE id = ?", (booking_id,))
            row = await cursor.fetchone()
            if row:
                return {"user_id": row[0], "target_date": row[1], "table_type": row[2]}
            return None

    @staticmethod
    async def get_booked_tables(target_date: str) -> list[str]:
        """Возвращает список ID столов, которые заняты на указанную дату."""
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "SELECT table_type FROM bookings WHERE target_date = ? AND status IN ('pending', 'confirmed')",
                (target_date,)
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


# ==================== FSM & CALLBACK DATA ====================
class BookingState(StatesGroup):
    waiting_for_receipt = State()

class DateCB(CallbackData, prefix="date"):
    value: str

class TableCB(CallbackData, prefix="table"):
    type: str

class ManagerCB(CallbackData, prefix="mgr"):
    action: str  # 'approve', 'reject', 'cancel'
    booking_id: int


# ==================== БИЗНЕС-ЛОГИКА (Даты) ====================
def get_reservation_dates() -> tuple[str, str]:
    """Рассчитывает даты Пятницы и Субботы."""
    tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    
    if now.weekday() == 6:
        days_until_friday = 5
    else:
        days_until_friday = 4 - now.weekday()
        
    friday = now + timedelta(days=days_until_friday)
    saturday = friday + timedelta(days=1)
    
    return friday.strftime("%d.%m.%Y"), saturday.strftime("%d.%m.%Y")

# ==================== ХЭНДЛЕРЫ КЛИЕНТА (Навигация) ====================

async def send_start_message(message_or_callback, state: FSMContext, edit=False):
    """Отправляет или редактирует стартовое сообщение (Главное меню)."""
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 Забронировать стол", callback_data="start_booking")]])
    text = (
        f"Привет, {message_or_callback.from_user.first_name}! Это бот для брони в баре KVARTAL 29.\n\n"
        "Мы работаем по Пятницам и Субботам. Нажмите кнопку ниже, чтобы забронировать столик."
    )
    if edit:
        await message_or_callback.message.edit_text(text, reply_markup=kb)
    else:
        await message_or_callback.answer(text, reply_markup=kb)

async def send_dates_message(message_or_callback, is_photo=False):
    """Отправляет меню выбора дат."""
    friday_str, saturday_str = get_reservation_dates()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Пятница ({friday_str})", callback_data=DateCB(value=friday_str).pack())],
        [InlineKeyboardButton(text=f"Суббота ({saturday_str})", callback_data=DateCB(value=saturday_str).pack())],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])
    text = "Выберите дату бронирования:"
    
    if is_photo:
        # Если мы возвращаемся с этапа выбора стола (где было фото плана зала)
        await message_or_callback.message.delete()
        await message_or_callback.message.answer(text, reply_markup=kb)
    else:
        # Если мы просто идем вперед со стартового сообщения (редактируем текст)
        await message_or_callback.message.edit_text(text, reply_markup=kb)


# ==================== ХЭНДЛЕРЫ КЛИЕНТА ====================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await send_start_message(message, state, edit=False)

@router.callback_query(F.data == "back_to_start")
async def process_back_to_start(callback: CallbackQuery, state: FSMContext):
    """Кнопка 'Назад' на этапе выбора даты -> Возврат в Главное меню."""
    await send_start_message(callback, state, edit=True)
    await callback.answer()

@router.callback_query(F.data == "start_booking")
async def process_start_booking(callback: CallbackQuery):
    await send_dates_message(callback, is_photo=False)
    await callback.answer()

@router.callback_query(F.data == "back_to_dates")
async def process_back_to_dates(callback: CallbackQuery):
    """Кнопка 'Назад' на этапе плана зала -> Возврат к выбору даты."""
    await send_dates_message(callback, is_photo=True)
    await callback.answer()


@router.callback_query(DateCB.filter())
async def process_date_selection(callback: CallbackQuery, callback_data: DateCB, state: FSMContext):
    await state.update_data(selected_date=callback_data.value)
    
    # Получаем список занятых столов на выбранную дату
    booked_tables = await Database.get_booked_tables(callback_data.value)
    
    # Динамически собираем клавиатуру
    builder = InlineKeyboardBuilder()
    
    for i in range(1, 12): # Теперь 11 столов
        t_id = str(i)
        table_info = TABLES[t_id]
        
        if not table_info["available"]:
            # Стол недоступен для брони (Бизнес-стол)
            builder.button(text=f"Стол {i} ({table_info['category']}) 🚫", callback_data="table_unavailable")
        elif t_id in booked_tables:
            # Стол занят
            builder.button(text=f"Стол {i} - Занят 🚫", callback_data="table_booked")
        else:
            # Стол свободен
            price = table_info['price']
            builder.button(text=f"Стол {i} - {price} руб.", callback_data=TableCB(type=t_id).pack())
            
    # Устанавливаем по 2 кнопки в ряд
    builder.adjust(2)
    # Добавляем кнопку "Назад" отдельной строкой
    builder.row(InlineKeyboardButton(text="⬅️ Назад к выбору даты", callback_data="back_to_dates"))
    
    # Удаляем старое текстовое сообщение, чтобы отправить фото с кнопками
    await callback.message.delete()
    
    # Отправляем план зала и кнопки
    photo = FSInputFile("floor_plan.jpg")
    await callback.message.answer_photo(
        photo=photo,
        caption=f"Вы выбрали <b>{callback_data.value}</b>.\nИзучите план зала и выберите свободный стол ниже:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@router.callback_query(F.data == "table_booked")
async def process_booked_table(callback: CallbackQuery):
    """Хэндлер для обработки клика по занятому столу."""
    await callback.answer("Этот стол уже забронирован на эту дату. Пожалуйста, выберите другой.", show_alert=True)

@router.callback_query(F.data == "table_unavailable")
async def process_unavailable_table(callback: CallbackQuery):
    """Хэндлер для обработки клика по недоступному (Бизнес) столу."""
    await callback.answer("Этот стол (Категория 'Бизнес') временно недоступен для бронирования через бота.", show_alert=True)

@router.callback_query(TableCB.filter())
async def process_table_selection(callback: CallbackQuery, callback_data: TableCB, state: FSMContext):
    await state.update_data(selected_table=callback_data.type)
    data = await state.get_data()
    
    date_str = data['selected_date']
    table_id = callback_data.type
    table_info = TABLES[table_id]
    price = table_info['price']
    category = table_info['category']
    video_path = table_info['video']
    
    text = (
        f"Вы выбрали <b>Стол {table_id}</b> (Категория: <b>{category}</b>) на <b>{date_str}</b>.\n"
        f"Для того чтобы забронировать столик, надо будет внести депозит <b>{price} руб.</b> на 89115952123 - Ozon банк, его вы сможете потратить в течении вечера.\n\n"
        "И у нас есть несколько правил:\n"
        "• Все обязательно должны при себе иметь оригинал паспорта\n"
        "• Придти вовремя (если задерживаетесь, то предупредить)\n"
        "• Так же нужно прийти до 00:00 так как после 00:00 сгорает депозит.\n"
        "Если эти пункты не будут соблюдены, то депозит не возвращается.\n\n"
        "Также будьте готовы пройти фейсконтроль, если по какой-то причине администратор входа считает что вы его не проходите, а предыдущие пункты соблюдены, то мы вернем вам депозит.\n\n"
        "📸 <b>После перевода отправьте фото или скриншот чека в этот чат.</b>"
    )
    
    # Удаляем фото плана зала
    await callback.message.delete()
    
    # Проверяем, существует ли файл с видео локально
    if os.path.exists(video_path):
        video = FSInputFile(video_path)
        await callback.message.answer_video(video=video, caption=text)
    else:
        # Если файла нет, отправляем просто текстом (fallback)
        await callback.message.answer(text)
    
    await state.set_state(BookingState.waiting_for_receipt)
    await callback.answer()


@router.message(BookingState.waiting_for_receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    date_str = data['selected_date']
    table_type = data['selected_table']
    table_name = TABLES[table_type]['name']
    table_category = TABLES[table_type]['category']
    
    username = message.from_user.username or message.from_user.first_name
    
    booking_id = await Database.add_booking(
        user_id=message.from_user.id,
        username=username,
        target_date=date_str,
        table_type=table_type
    )
    
    await message.answer("✅ Чек получен! Ожидайте подтверждения менеджера. Я пришлю уведомление.")
    await state.clear()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=ManagerCB(action="approve", booking_id=booking_id).pack()),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=ManagerCB(action="reject", booking_id=booking_id).pack())
        ]
    ])
    
    caption = (
        f"🚨 <b>Новая бронь!</b>\n"
        f"👤 Клиент: @{username} (ID: <code>{message.from_user.id}</code>)\n"
        f"📅 Дата: <b>{date_str}</b>\n"
        f"🪑 Стол: <b>{table_name} ({table_category})</b>\n"
        f"🆔 ID заявки: {booking_id}"
    )
    
    await bot.send_photo(
        chat_id=MANAGER_CHAT_ID,
        photo=message.photo[-1].file_id,
        caption=caption,
        reply_markup=kb
    )

@router.message(BookingState.waiting_for_receipt)
async def process_receipt_invalid(message: Message):
    await message.answer("Пожалуйста, отправьте именно <b>фотографию</b> (или скриншот) чека.")


# ==================== ХЭНДЛЕРЫ МЕНЕДЖЕРА ====================
@router.callback_query(ManagerCB.filter())
async def process_manager_action(callback: CallbackQuery, callback_data: ManagerCB, bot: Bot):
    action = callback_data.action
    booking_id = callback_data.booking_id
    
    booking = await Database.get_booking(booking_id)
    if not booking:
        await callback.answer("Ошибка: Бронь не найдена в БД!", show_alert=True)
        return
        
    user_id = booking['user_id']
    date_str = booking['target_date']
    table_id = booking['table_type']
    
    caption_lines = callback.message.caption.split('\n')
    base_caption = "\n".join(caption_lines[:5]) 
    
    if action == "approve":
        await Database.update_status(booking_id, "confirmed")
        
        # Добавляем кнопку "Снять бронь" после подтверждения
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Снять бронь", callback_data=ManagerCB(action="cancel", booking_id=booking_id).pack())
        ]])
        
        new_caption = f"{base_caption}\n\n✅ <b>Подтверждено менеджером</b>"
        await callback.message.edit_caption(caption=new_caption, reply_markup=kb)
        
        try:
            await bot.send_message(user_id, f"🎉 Ваша бронь на <b>Стол {table_id}</b> ({date_str}) успешно подтверждена! Ждем вас в нашем баре KVARTAL 29, по адресу Архангельск, Троицкий проспект, 122. По всем вопросам @waozixfu")
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение клиенту {user_id}: {e}")
            
    elif action == "reject":
        await Database.update_status(booking_id, "rejected")
        
        new_caption = f"{base_caption}\n\n❌ <b>Отклонено</b>"
        await callback.message.edit_caption(caption=new_caption, reply_markup=None)
        
        try:
            await bot.send_message(user_id, "😔 К сожалению, ваша бронь отклонена. Пожалуйста, проверьте платеж или свяжитесь с поддержкой @waozixfu.")
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение клиенту {user_id}: {e}")
            
    elif action == "cancel":
        await Database.update_status(booking_id, "cancelled")
        
        new_caption = f"{base_caption}\n\n⚠️ <b>Бронь снята менеджером</b>"
        await callback.message.edit_caption(caption=new_caption, reply_markup=None)
        
        try:
            await bot.send_message(user_id, f"⚠️ Ваша бронь на <b>Стол {table_id}</b> ({date_str}) была отменена менеджером. Если у вас есть вопросы, пожалуйста, свяжитесь с поддержкой @waozixfu.")
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение клиенту {user_id}: {e}")

    await callback.answer()


# ==================== ЗАПУСК БОТА ====================
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    
    await Database.init_db()
    
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    
    logging.info("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")