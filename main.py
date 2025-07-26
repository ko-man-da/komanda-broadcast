import asyncio
import logging
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import Chat, ChatMember
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID =  os.getenv("ADMIN_ID")
TARGET_CHAT_ID =  os.getenv("TARGET_CHAT_ID")

# Конфигурация базы данных
DB_CONFIG = {
    'database': os.getenv("DB_NAME", "invite_bot"),
    'user': os.getenv("DB_USER", "postgres"),
    'password': os.getenv("DB_PASSWORD", "wellcometotheworld"),
    'host': os.getenv("DB_HOST", "localhost"),
    'port': int(os.getenv("DB_PORT", "5432"))
}


# Настройки рассылки
BROADCAST_SETTINGS = {
    "to_target_chat": True,  # Отправлять в целевой чат
    "to_target_chat_members": True,  # Отправлять участникам целевого чата в личку
    "to_network_chats": True,  # Отправлять в сетку чатов
    "network_chat_mode": "all",  # "all", "members_only", "specific_chats"
    "selected_chats": [],  # Конкретные выбранные чаты
    "available_chats": {}  # Доступные чаты где есть бот
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_pool = None


# Состояния для FSM
class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    confirming_broadcast = State()
    selecting_network_chats = State()


# Инициализация базы данных
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(**DB_CONFIG)

    async with db_pool.acquire() as conn:
        # Создание таблицы пользователей
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                is_bot BOOLEAN DEFAULT FALSE,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Создание таблицы участников чатов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_members (
                user_id BIGINT,
                chat_id BIGINT,
                status VARCHAR(50),
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')

        # Создание таблицы чатов бота
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_chats (
                chat_id BIGINT PRIMARY KEY,
                chat_title VARCHAR(500),
                chat_type VARCHAR(50),
                members_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Создание индексов для оптимизации
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_is_bot ON users (is_bot)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_members_chat_id ON chat_members (chat_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_members_user_id ON chat_members (user_id)')


# Функции для работы с базой данных
async def add_user(user_id: int, username: str = None, first_name: str = None,
                   last_name: str = None, is_bot: bool = False):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, is_bot)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                is_bot = EXCLUDED.is_bot
        ''', user_id, username, first_name, last_name, is_bot)


async def add_chat_member(user_id: int, chat_id: int, status: str = "member"):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO chat_members (user_id, chat_id, status)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, chat_id) DO UPDATE SET
                status = EXCLUDED.status,
                joined_date = CURRENT_TIMESTAMP
        ''', user_id, chat_id, status)


async def remove_chat_member(user_id: int, chat_id: int):
    """Удаляет пользователя из участников чата"""
    async with db_pool.acquire() as conn:
        await conn.execute('''
            DELETE FROM chat_members 
            WHERE user_id = $1 AND chat_id = $2
        ''', user_id, chat_id)


async def add_bot_chat(chat_id: int, title: str, chat_type: str, members_count: int = 0):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO bot_chats (chat_id, chat_title, chat_type, members_count, last_updated)
            VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
            ON CONFLICT (chat_id) DO UPDATE SET
                chat_title = EXCLUDED.chat_title,
                chat_type = EXCLUDED.chat_type,
                members_count = EXCLUDED.members_count,
                last_updated = CURRENT_TIMESTAMP
        ''', chat_id, title, chat_type, members_count)


async def get_all_users() -> List[int]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id FROM users WHERE is_bot = FALSE')
        return [row['user_id'] for row in rows]


async def get_chat_members(chat_id: int) -> List[int]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT user_id FROM chat_members WHERE chat_id = $1', chat_id)
        return [row['user_id'] for row in rows]


# НОВАЯ функция: получение только участников целевого чата, которые начали диалог с ботом
async def get_target_chat_users() -> List[int]:
    """Возвращает список пользователей, которые являются участниками целевого чата И начали диалог с ботом"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT DISTINCT cm.user_id 
            FROM chat_members cm
            JOIN users u ON cm.user_id = u.user_id
            WHERE cm.chat_id = $1 AND u.is_bot = FALSE
        ''', TARGET_CHAT_ID)
        return [row['user_id'] for row in rows]


async def get_bot_chats() -> Dict[int, Dict[str, Any]]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('SELECT chat_id, chat_title, chat_type, members_count FROM bot_chats')
        chats = {}
        for row in rows:
            chats[row['chat_id']] = {
                'title': row['chat_title'],
                'type': row['chat_type'],
                'members_count': row['members_count']
            }
        return chats


async def remove_bot_chat(chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM bot_chats WHERE chat_id = $1', chat_id)


async def get_user_statistics():
    async with db_pool.acquire() as conn:
        # Общее количество пользователей
        users_count = await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_bot = FALSE')

        # Участники целевого чата
        target_chat_members = await conn.fetchval(
            'SELECT COUNT(*) FROM chat_members WHERE chat_id = $1', TARGET_CHAT_ID
        )

        # Участники целевого чата, которые начали диалог с ботом
        target_chat_users = await conn.fetchval('''
            SELECT COUNT(DISTINCT cm.user_id) 
            FROM chat_members cm
            JOIN users u ON cm.user_id = u.user_id
            WHERE cm.chat_id = $1 AND u.is_bot = FALSE
        ''', TARGET_CHAT_ID)

        # Количество доступных чатов
        chats_count = await conn.fetchval('SELECT COUNT(*) FROM bot_chats')

        # Топ 5 чатов по количеству участников
        top_chats = await conn.fetch('''
            SELECT chat_title, members_count 
            FROM bot_chats 
            ORDER BY members_count DESC 
            LIMIT 5
        ''')

        return {
            'users_count': users_count,
            'target_chat_members': target_chat_members,
            'target_chat_users': target_chat_users,
            'chats_count': chats_count,
            'top_chats': top_chats
        }


# Функция синхронизации участников целевого чата
async def sync_target_chat_members(progress_callback=None):
    """
    Синхронизирует участников целевого чата с базой данных.
    Удаляет из базы пользователей, которые покинули чат.
    """
    try:
        if progress_callback:
            await progress_callback("🔄 Получение списка участников чата...")

        # Получаем текущий список участников из Telegram API
        current_members = set()

        try:
            # Получаем информацию о чате
            chat_info = await bot.get_chat(TARGET_CHAT_ID)

            # Для публичных чатов или каналов, получаем участников через administrators
            try:
                administrators = await bot.get_chat_administrators(TARGET_CHAT_ID)
                for admin in administrators:
                    if not admin.user.is_bot:
                        current_members.add(admin.user.id)
            except Exception:
                pass

            # Для супергрупп пытаемся получить больше информации
            if chat_info.type == ChatType.SUPERGROUP:
                try:
                    # Получаем количество участников
                    member_count = await bot.get_chat_member_count(TARGET_CHAT_ID)
                    if progress_callback:
                        await progress_callback(f"📊 В чате {member_count} участников")

                    # Обновляем информацию о чате
                    await add_bot_chat(TARGET_CHAT_ID, chat_info.title, chat_info.type, member_count)

                except Exception as e:
                    logging.warning(f"Не удалось получить количество участников: {e}")

            # Получаем список участников из базы данных
            db_members = await get_chat_members(TARGET_CHAT_ID)

            if progress_callback:
                await progress_callback(f"🔍 Проверка {len(db_members)} участников из базы данных...")

            # Проверяем каждого участника из базы данных
            removed_count = 0
            for user_id in db_members:
                try:
                    # Проверяем статус пользователя в чате
                    member = await bot.get_chat_member(TARGET_CHAT_ID, user_id)

                    if member.status in ['left', 'kicked']:
                        # Пользователь покинул чат или был исключен
                        await remove_chat_member(user_id, TARGET_CHAT_ID)
                        removed_count += 1
                        logging.info(f"Удален пользователь {user_id} из участников чата {TARGET_CHAT_ID}")
                    else:
                        # Пользователь все еще в чате, обновляем статус
                        await add_chat_member(user_id, TARGET_CHAT_ID, member.status)
                        current_members.add(user_id)

                    # Небольшая задержка для избежания лимитов API
                    await asyncio.sleep(0.1)

                except Exception as e:
                    # Если не можем получить информацию о пользователе,
                    # скорее всего он покинул чат
                    await remove_chat_member(user_id, TARGET_CHAT_ID)
                    removed_count += 1
                    logging.warning(f"Удален пользователь {user_id} (ошибка API): {e}")

            if progress_callback:
                await progress_callback(f"✅ Синхронизация завершена. Удалено неактивных участников: {removed_count}")

            return {
                'removed_count': removed_count,
                'current_members': len(current_members),
                'db_members_before': len(db_members)
            }

        except Exception as e:
            logging.error(f"Ошибка при получении информации о чате {TARGET_CHAT_ID}: {e}")
            if progress_callback:
                await progress_callback(f"❌ Ошибка при проверке чата: {e}")
            return None

    except Exception as e:
        logging.error(f"Общая ошибка синхронизации: {e}")
        if progress_callback:
            await progress_callback(f"❌ Ошибка синхронизации: {e}")
        return None


# Обновление информации о чатах
async def update_available_chats():
    """Обновляет список доступных чатов где есть бот"""
    try:
        # Получаем текущие чаты из базы данных
        bot_chats = await get_bot_chats()

        updated_chats = {}
        removed_count = 0

        if not bot_chats:
            logging.info("Нет чатов в базе данных для обновления")
            BROADCAST_SETTINGS['available_chats'] = {}
            return

        # Проверяем актуальность каждого чата из базы
        for chat_id in list(bot_chats.keys()):
            try:
                # Пытаемся получить информацию о чате
                chat_info = await bot.get_chat(chat_id)

                # Проверяем, является ли бот участником чата
                try:
                    bot_member = await bot.get_chat_member(chat_id, bot.id)

                    # Если бот покинул чат или был исключен
                    if bot_member.status in ['left', 'kicked']:
                        await remove_bot_chat(chat_id)
                        removed_count += 1
                        logging.info(f"Удален чат {chat_id} - бот больше не участник")
                        continue

                    # Получаем актуальное количество участников
                    members_count = 0
                    if chat_info.type in ['group', 'supergroup']:
                        try:
                            members_count = await bot.get_chat_member_count(chat_id)
                        except Exception as e:
                            logging.warning(f"Не удалось получить количество участников чата {chat_id}: {e}")
                            members_count = bot_chats[chat_id].get('members_count', 0)  # Используем старое значение

                    # Обновляем информацию о чате
                    chat_type_str = get_chat_type_string(chat_info.type)
                    await add_bot_chat(chat_id, chat_info.title or f"Chat {chat_id}", chat_type_str, members_count)
                    updated_chats[chat_id] = {
                        'title': chat_info.title or f"Chat {chat_id}",
                        'type': chat_info.type,
                        'members_count': members_count
                    }

                except Exception as e:
                    # Если ошибка связана с правами доступа, проверяем через другой метод
                    if "member not found" in str(e).lower() or "chat not found" in str(e).lower():
                        await remove_bot_chat(chat_id)
                        removed_count += 1
                        logging.warning(f"Удален чат {chat_id} - бот не найден в чате: {e}")
                    else:
                        # Для других ошибок оставляем чат, но логируем проблему
                        logging.warning(f"Ошибка при проверке статуса бота в чате {chat_id}: {e}")
                        # Сохраняем чат с текущей информацией
                        updated_chats[chat_id] = bot_chats[chat_id]

            except Exception as e:
                # Если чат полностью недоступен
                if "chat not found" in str(e).lower():
                    await remove_bot_chat(chat_id)
                    removed_count += 1
                    logging.warning(f"Удален недоступный чат {chat_id}: {e}")
                else:
                    logging.error(f"Неожиданная ошибка при обработке чата {chat_id}: {e}")

        # Обновляем глобальную переменную
        BROADCAST_SETTINGS['available_chats'] = updated_chats

        logging.info(f"Обновление чатов завершено. Актуальных: {len(updated_chats)}, удалено: {removed_count}")

    except Exception as e:
        logging.error(f"Общая ошибка при обновлении чатов: {e}")


def get_chat_type_string(chat_type):
    """Безопасно получает строковое представление типа чата"""
    if hasattr(chat_type, 'value'):
        return chat_type.value
    return str(chat_type)


async def update_chat_info_from_message(message: Message):
    """Обновляет информацию о чате на основе полученного сообщения"""
    try:
        if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            members_count = await bot.get_chat_member_count(message.chat.id)
            chat_type_str = get_chat_type_string(message.chat.type)

            await add_bot_chat(
                message.chat.id,
                message.chat.title,
                chat_type_str,
                members_count
            )

            # Обновляем глобальную переменную
            BROADCAST_SETTINGS['available_chats'][message.chat.id] = {
                'title': message.chat.title,
                'type': chat_type_str,
                'members_count': members_count
            }

            logging.info(f"Обновлена информация о чате {message.chat.id}: {message.chat.title}")

    except Exception as e:
        logging.warning(f"Не удалось обновить информацию о чате {message.chat.id}: {e}")


# Проверка на админа
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ОБНОВЛЕННАЯ функция для подсчета целей рассылки
async def calculate_broadcast_targets():
    """Подсчитывает количество целей для рассылки (только участники целевого чата)"""
    total_targets = 0
    details = []

    # Участники целевого чата в личку
    if BROADCAST_SETTINGS['to_target_chat_members']:
        target_users = await get_target_chat_users()
        total_targets += len(target_users)
        details.append(f"👤 Участники целевого чата (в личку): {len(target_users)} пользователей")

    # Целевой чат
    if BROADCAST_SETTINGS['to_target_chat']:
        total_targets += 1
        details.append(f"💬 Целевой чат: 1 чат")

    # Сетка чатов
    if BROADCAST_SETTINGS['to_network_chats']:
        if BROADCAST_SETTINGS['network_chat_mode'] == "all":
            chat_count = len(BROADCAST_SETTINGS['available_chats'])
            total_targets += chat_count
            details.append(f"🌐 Все доступные чаты: {chat_count} чатов")
        elif BROADCAST_SETTINGS['network_chat_mode'] == "members_only":
            # Участники всех чатов сетки, но только те, кто в целевом чате
            all_members = set()
            for chat_id in BROADCAST_SETTINGS['available_chats'].keys():
                members = await get_chat_members(chat_id)
                all_members.update(members)

            # Фильтруем только участников целевого чата
            target_chat_members = set(await get_target_chat_users())
            filtered_members = all_members.intersection(target_chat_members)

            total_targets += len(filtered_members)
            details.append(f"👥 Участники чатов (только из целевого чата): {len(filtered_members)} пользователей")
        elif BROADCAST_SETTINGS['network_chat_mode'] == "specific_chats":
            selected_count = len(BROADCAST_SETTINGS['selected_chats'])
            total_targets += selected_count
            details.append(f"🎯 Выбранные чаты: {selected_count} чатов")

    return total_targets, details


# Создание админ клавиатуры
def get_admin_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать рассылку", callback_data="create_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки рассылки", callback_data="broadcast_settings")],
        [InlineKeyboardButton(text="🔄 Обновить чаты", callback_data="update_chats")],
        [InlineKeyboardButton(text="🔄 Синхронизация участников", callback_data="sync_members")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="statistics")]
    ])
    return keyboard


# ОБНОВЛЕННАЯ клавиатура настроек
def get_settings_keyboard() -> InlineKeyboardMarkup:
    mode_text = {
        "all": "Все чаты",
        "members_only": "Только участникам из целевого чата",
        "specific_chats": "Выбранные чаты"
    }

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"👤 Участникам целевого чата: {'✅' if BROADCAST_SETTINGS['to_target_chat_members'] else '❌'}",
            callback_data="toggle_target_members"
        )],
        [InlineKeyboardButton(
            text=f"💬 Целевой чат: {'✅' if BROADCAST_SETTINGS['to_target_chat'] else '❌'}",
            callback_data="toggle_target_chat"
        )],
        [InlineKeyboardButton(
            text=f"🌐 Сетка чатов: {'✅' if BROADCAST_SETTINGS['to_network_chats'] else '❌'}",
            callback_data="toggle_network"
        )],
        [InlineKeyboardButton(
            text=f"📋 Режим сетки: {mode_text.get(BROADCAST_SETTINGS['network_chat_mode'], 'Неизвестно')}",
            callback_data="change_network_mode"
        )],
        [InlineKeyboardButton(text="🎯 Выбрать чаты", callback_data="select_chats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
    ])
    return keyboard


# Создание клавиатуры режимов сетки
def get_network_mode_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Все доступные чаты", callback_data="mode_all")],
        [InlineKeyboardButton(text="👥 Только участникам из целевого чата", callback_data="mode_members_only")],
        [InlineKeyboardButton(text="🎯 Выбранные чаты", callback_data="mode_specific")],
        [InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="broadcast_settings")]
    ])
    return keyboard


# Функция создания клавиатуры выбора чатов
def get_chat_selection_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    chats = BROADCAST_SETTINGS['available_chats']
    selected = BROADCAST_SETTINGS['selected_chats']

    buttons = []
    chats_per_page = 6
    start_idx = page * chats_per_page
    end_idx = start_idx + chats_per_page

    chat_items = list(chats.items())[start_idx:end_idx]

    for chat_id, chat_info in chat_items:
        is_selected = chat_id in selected
        title = chat_info['title'][:25] + "..." if len(chat_info['title']) > 25 else chat_info['title']

        button_text = f"{'✅' if is_selected else '❌'} {title}"
        buttons.append([InlineKeyboardButton(
            text=button_text,
            callback_data=f"toggle_chat_{chat_id}"
        )])

    # Управляющие кнопки
    control_buttons = []
    control_buttons.append([
        InlineKeyboardButton(text="✅ Выбрать все", callback_data="select_all_chats"),
        InlineKeyboardButton(text="❌ Очистить", callback_data="clear_selected_chats")
    ])

    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Предыдущая", callback_data=f"chat_page_{page - 1}"))
    if end_idx < len(chats):
        nav_buttons.append(InlineKeyboardButton(text="▶️ Следующая", callback_data=f"chat_page_{page + 1}"))

    if nav_buttons:
        control_buttons.append(nav_buttons)

    control_buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="chat_selection_done")])

    buttons.extend(control_buttons)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Создание клавиатуры подтверждения рассылки
def get_confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отправить", callback_data="confirm_broadcast"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_broadcast")
        ],
        [InlineKeyboardButton(text="📝 Изменить текст", callback_data="edit_broadcast_text")]
    ])
    return keyboard


# Обработчики команд
@dp.message(CommandStart())
async def start_handler(message: Message):
    user = message.from_user
    await add_user(user.id, user.username, user.first_name, user.last_name, user.is_bot)

    # Если это групповой чат, сохраняем информацию о чате
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            members_count = await bot.get_chat_member_count(message.chat.id)
            await add_bot_chat(message.chat.id, message.chat.title, get_chat_type_string(message.chat.type), members_count)

            await add_chat_member(user.id, message.chat.id)
        except Exception as e:
            logging.warning(f"Не удалось получить информацию о чате {message.chat.id}: {e}")

    if is_admin(user.id):
        await update_available_chats()
        await message.answer(
            "🔐 **Добро пожаловать в админ панель!**\n\n"
            "Выберите действие:",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )
    else:
        # Проверяем, является ли пользователь участником целевого чата
        target_chat_members = await get_chat_members(TARGET_CHAT_ID)
        if user.id in target_chat_members:
            await message.answer(
                f"👋 Привет, {user.first_name}!\n\n"
                "Вы участник целевого чата и подписались на получение уведомлений от бота."
            )
        else:
            await message.answer(
                f"👋 Привет, {user.first_name}!\n\n"
                "⚠️ Для получения рассылок вам необходимо быть участником нашего чата."
            )


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав доступа к админ панели.")
        return

    await update_available_chats()
    await message.answer(
        "🔐 **Админ панель**\n\nВыберите действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )


# Обработчики callback кнопок
@dp.callback_query(F.data == "create_broadcast")
async def create_broadcast_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "📝 **Создание рассылки**\n\n"
        "Отправьте текст сообщения для рассылки.\n"
        "Рассылка будет отправлена **только участникам целевого чата**.\n\n"
        "Вы можете использовать Markdown разметку.\n\n"
        "Отправьте /cancel для отмены.",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_broadcast_text)
    await callback.answer()


@dp.callback_query(F.data == "broadcast_settings")
async def settings_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    mode_text = {
        "all": "Все чаты",
        "members_only": "Только участникам из целевого чата",
        "specific_chats": "Выбранные чаты"
    }

    settings_text = (
        "⚙️ **Настройки рассылки**\n\n"
        f"👤 Участникам целевого чата: {'✅ Включено' if BROADCAST_SETTINGS['to_target_chat_members'] else '❌ Отключено'}\n"
        f"💬 Целевой чат: {'✅ Включено' if BROADCAST_SETTINGS['to_target_chat'] else '❌ Отключено'}\n"
        f"🌐 Сетка чатов: {'✅ Включено' if BROADCAST_SETTINGS['to_network_chats'] else '❌ Отключено'}\n"
        f"📋 Режим сетки: {mode_text.get(BROADCAST_SETTINGS['network_chat_mode'], 'Неизвестно')}\n"
        f"🎯 Выбранных чатов: {len(BROADCAST_SETTINGS['selected_chats'])}\n"
        f"📊 Доступных чатов: {len(BROADCAST_SETTINGS['available_chats'])}\n\n"
        f"ℹ️ **Важно:** Рассылка отправляется только участникам целевого чата!"
    )

    await callback.message.edit_text(
        settings_text,
        reply_markup=get_settings_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "update_chats")
async def update_chats_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    await callback.answer("🔄 Обновляю список чатов...")

    # Создаем прогресс сообщение
    progress_msg = await callback.message.edit_text(
        "🔄 **Обновление списка чатов**\n\n"
        "Проверяю актуальность чатов из базы данных...",
        parse_mode="Markdown"
    )

    # Получаем количество чатов до обновления
    chats_before = len(BROADCAST_SETTINGS['available_chats'])

    # Запускаем обновление
    await update_available_chats()

    # Получаем количество чатов после обновления
    chats_after = len(BROADCAST_SETTINGS['available_chats'])

    # Формируем отчет
    if chats_after == 0:
        report_text = (
            f"📋 **Обновление чатов завершено**\n\n"
            f"❌ Не найдено доступных чатов\n\n"
            f"**Возможные причины:**\n"
            f"• Бот не добавлен ни в один чат\n"
            f"• Бот был исключен из всех чатов\n"
            f"• Необходимо добавить бота в чаты\n\n"
            f"**Как добавить чаты:**\n"
            f"1. Добавьте бота в групповые чаты\n"
            f"2. Отправьте /start в чате с ботом\n"
            f"3. Нажмите 'Обновить чаты' снова"
        )
    else:
        chats_info = ""
        for chat_id, chat_info in list(BROADCAST_SETTINGS['available_chats'].items())[:5]:
            title = chat_info['title'][:25] + "..." if len(chat_info['title']) > 25 else chat_info['title']
            chats_info += f"• {title} ({chat_info['members_count']} участников)\n"

        if len(BROADCAST_SETTINGS['available_chats']) > 5:
            chats_info += f"• ...и еще {len(BROADCAST_SETTINGS['available_chats']) - 5} чатов\n"

        report_text = (
            f"✅ **Обновление чатов завершено**\n\n"
            f"📊 Было чатов: {chats_before}\n"
            f"📊 Стало чатов: {chats_after}\n"
            f"🔄 Изменение: {chats_after - chats_before:+d}\n\n"
            f"**Доступные чаты:**\n{chats_info}"
        )

    await progress_msg.edit_text(
        report_text,
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )


@dp.message(F.content_type.in_({'new_chat_members'}))
async def new_member_handler(message: Message):
    """Отслеживание новых участников в чатах"""
    # Проверяем, не добавили ли бота в чат
    bot_added = any(user.id == bot.id for user in message.new_chat_members)

    for user in message.new_chat_members:
        if not user.is_bot:
            await add_chat_member(user.id, message.chat.id, "member")
            await add_user(user.id, user.username, user.first_name, user.last_name, user.is_bot)

    # Обновляем информацию о чате
    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update_chat_info_from_message(message)

        # Если бота только что добавили в чат, логируем это
        if bot_added:
            logging.info(f"Бот добавлен в новый чат {message.chat.id}: {message.chat.title}")


@dp.callback_query(F.data == "statistics")
async def statistics_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    stats = await get_user_statistics()

    chats_info = ""
    if BROADCAST_SETTINGS['available_chats']:
        for chat_id, chat_info in list(BROADCAST_SETTINGS['available_chats'].items())[:5]:
            title = chat_info['title'][:20] + "..." if len(chat_info['title']) > 20 else chat_info['title']
            chats_info += f"• {title} ({chat_info['members_count']} участников)\n"

        if len(BROADCAST_SETTINGS['available_chats']) > 5:
            chats_info += f"• ...и еще {len(BROADCAST_SETTINGS['available_chats']) - 5} чатов\n"
    else:
        chats_info = "❌ Нет доступных чатов\n\n**Для добавления чатов:**\n1. Добавьте бота в групповые чаты\n2. Отправьте /start в чате\n3. Обновите список чатов"

    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей (начавших диалог): {stats['users_count']}\n"
        f"💬 Участников целевого чата: {stats['target_chat_members']}\n"
        f"🎯 Участников целевого чата с диалогом: {stats['target_chat_users']}\n"
        f"🌐 Доступных чатов: {len(BROADCAST_SETTINGS['available_chats'])}\n"
        f"📝 Выбранных чатов: {len(BROADCAST_SETTINGS['selected_chats'])}\n"
        f"🆔 ID целевого чата: `{TARGET_CHAT_ID}`\n\n"
        f"**Доступные чаты:**\n{chats_info}\n\n"
        f"ℹ️ **Важно:** Рассылка отправляется только участникам целевого чата!"
    )

    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
    ])

    await callback.message.edit_text(
        stats_text,
        reply_markup=back_keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

# ОБНОВЛЕННЫЕ обработчики настроек
@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_settings_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    setting = callback.data.replace("toggle_", "")

    if setting == "target_members":
        BROADCAST_SETTINGS['to_target_chat_members'] = not BROADCAST_SETTINGS['to_target_chat_members']
    elif setting == "target_chat":
        BROADCAST_SETTINGS['to_target_chat'] = not BROADCAST_SETTINGS['to_target_chat']
    elif setting == "network":
        BROADCAST_SETTINGS['to_network_chats'] = not BROADCAST_SETTINGS['to_network_chats']

    await callback.answer("✅ Настройка изменена!")
    await settings_handler(callback)


@dp.callback_query(F.data == "change_network_mode")
async def change_network_mode_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "📋 **Выберите режим работы сетки чатов:**\n\n"
        "🌐 **Все доступные чаты** - рассылка во все чаты где есть бот\n"
        "👥 **Только участникам из целевого чата** - в личку участникам, которые есть и в других чатах\n"
        "🎯 **Выбранные чаты** - только в выбранные вами чаты\n\n"
        "ℹ️ **Важно:** В любом случае рассылка идет только участникам целевого чата!",
        reply_markup=get_network_mode_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mode_"))
async def set_network_mode_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    mode = callback.data.replace("mode_", "")
    BROADCAST_SETTINGS['network_chat_mode'] = mode

    await callback.answer("✅ Режим изменен!")
    await settings_handler(callback)


@dp.callback_query(F.data == "select_chats")
async def select_chats_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    if not BROADCAST_SETTINGS['available_chats']:
        await callback.answer("❌ Нет доступных чатов. Обновите список чатов.", show_alert=True)
        return

    await callback.message.edit_text(
        f"🎯 **Выберите чаты для рассылки**\n\n"
        f"Выбрано: {len(BROADCAST_SETTINGS['selected_chats'])}/{len(BROADCAST_SETTINGS['available_chats'])}\n\n"
        "Нажмите на чат, чтобы включить/исключить его из рассылки.",
        reply_markup=get_chat_selection_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


# Обработчики выбора чатов (без изменений)
@dp.callback_query(F.data.startswith("toggle_chat_"))
async def toggle_chat_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    chat_id = int(callback.data.replace("toggle_chat_", ""))

    if chat_id in BROADCAST_SETTINGS['selected_chats']:
        BROADCAST_SETTINGS['selected_chats'].remove(chat_id)
        status = "исключен"
    else:
        BROADCAST_SETTINGS['selected_chats'].append(chat_id)
        status = "добавлен"

    await callback.answer(f"✅ Чат {status}!")

    await callback.message.edit_text(
        f"🎯 **Выберите чаты для рассылки**\n\n"
        f"Выбрано: {len(BROADCAST_SETTINGS['selected_chats'])}/{len(BROADCAST_SETTINGS['available_chats'])}\n\n"
        "Нажмите на чат, чтобы включить/исключить его из рассылки.",
        reply_markup=get_chat_selection_keyboard(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("chat_page_"))
async def chat_page_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    page = int(callback.data.replace("chat_page_", ""))

    await callback.message.edit_text(
        f"🎯 **Выберите чаты для рассылки**\n\n"
        f"Выбрано: {len(BROADCAST_SETTINGS['selected_chats'])}/{len(BROADCAST_SETTINGS['available_chats'])}\n\n"
        "Нажмите на чат, чтобы включить/исключить его из рассылки.",
        reply_markup=get_chat_selection_keyboard(page),
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "select_all_chats")
async def select_all_chats_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    BROADCAST_SETTINGS['selected_chats'] = list(BROADCAST_SETTINGS['available_chats'].keys())
    await callback.answer(f"✅ Выбраны все чаты ({len(BROADCAST_SETTINGS['selected_chats'])})")

    await callback.message.edit_text(
        f"🎯 **Выберите чаты для рассылки**\n\n"
        f"Выбрано: {len(BROADCAST_SETTINGS['selected_chats'])}/{len(BROADCAST_SETTINGS['available_chats'])}\n\n"
        "Нажмите на чат, чтобы включить/исключить его из рассылки.",
        reply_markup=get_chat_selection_keyboard(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "clear_selected_chats")
async def clear_selected_chats_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    BROADCAST_SETTINGS['selected_chats'].clear()
    await callback.answer("❌ Выбор очищен")

    await callback.message.edit_text(
        f"🎯 **Выберите чаты для рассылки**\n\n"
        f"Выбрано: {len(BROADCAST_SETTINGS['selected_chats'])}/{len(BROADCAST_SETTINGS['available_chats'])}\n\n"
        "Нажмите на чат, чтобы включить/исключить его из рассылки.",
        reply_markup=get_chat_selection_keyboard(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "chat_selection_done")
async def chat_selection_done_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    selected_count = len(BROADCAST_SETTINGS['selected_chats'])
    await callback.answer(f"✅ Выбор завершен! Выбрано чатов: {selected_count}")
    await settings_handler(callback)


@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "🔐 **Админ панель**\n\nВыберите действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


# Обработчики подтверждения рассылки
@dp.callback_query(F.data == "confirm_broadcast")
async def confirm_broadcast_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    broadcast_text = data.get('broadcast_text')

    if not broadcast_text:
        await callback.answer("❌ Ошибка: текст рассылки не найден", show_alert=True)
        await state.clear()
        return

    await callback.answer("🚀 Запускаю рассылку...")
    await state.clear()

    await start_broadcast(callback.message, broadcast_text)


@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    await state.clear()
    await callback.answer("❌ Рассылка отменена")

    await callback.message.edit_text(
        "❌ **Рассылка отменена**\n\nВыберите действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "edit_broadcast_text")
async def edit_broadcast_text_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "📝 **Редактирование рассылки**\n\n"
        "Отправьте новый текст сообщения для рассылки.\n"
        "Рассылка будет отправлена **только участникам целевого чата**.\n\n"
        "Вы можете использовать Markdown разметку.\n\n"
        "Отправьте /cancel для отмены.",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_broadcast_text)
    await callback.answer()


# ОБНОВЛЕННЫЙ обработчик текста для рассылки
@dp.message(AdminStates.waiting_broadcast_text)
async def broadcast_text_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if message.text == "/cancel":
        await message.answer(
            "❌ Создание рассылки отменено.",
            reply_markup=get_admin_keyboard()
        )
        await state.clear()
        return

    broadcast_text = message.text
    await state.update_data(broadcast_text=broadcast_text)

    # Автоматическая синхронизация перед рассылкой
    sync_msg = await message.answer(
        "🔄 **Подготовка к рассылке**\n\n"
        "Проверяю актуальность списка участников целевого чата...",
        parse_mode="Markdown"
    )

    async def update_sync_progress(text):
        try:
            await sync_msg.edit_text(
                f"🔄 **Подготовка к рассылке**\n\n{text}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    sync_result = await sync_target_chat_members(update_sync_progress)

    # Подсчитываем цели рассылки после синхронизации
    total_targets, details = await calculate_broadcast_targets()

    if total_targets == 0:
        await sync_msg.edit_text(
            "⚠️ **Внимание!**\n\n"
            "Не найдено участников целевого чата для рассылки.\n"
            "Убедитесь, что:\n"
            "• Бот добавлен в целевой чат\n"
            "• Участники начали диалог с ботом\n"
            "• Настройки рассылки включены",
            reply_markup=get_admin_keyboard(),
            parse_mode="Markdown"
        )
        await state.clear()
        return

    # Формируем превью рассылки с информацией о синхронизации
    preview_text = f"📋 **Превью рассылки**\n\n"

    if sync_result:
        preview_text += f"🔄 **Синхронизация завершена:**\n"
        preview_text += f"• Удалено неактивных участников: {sync_result['removed_count']}\n\n"

    preview_text += f"**Текст сообщения:**\n{broadcast_text[:200]}{'...' if len(broadcast_text) > 200 else ''}\n\n"
    preview_text += f"**Цели рассылки (только участники целевого чата):**\n"
    preview_text += "\n".join(details)
    preview_text += f"\n\n**Всего будет отправлено:** {total_targets} сообщений\n\n"
    preview_text += "⚠️ **Вы уверены, что хотите отправить рассылку?**\n\n"
    preview_text += f"ℹ️ **Целевой чат:** `{TARGET_CHAT_ID}`"

    await sync_msg.edit_text(
        preview_text,
        reply_markup=get_confirmation_keyboard(),
        parse_mode="Markdown"
    )

    await state.set_state(AdminStates.confirming_broadcast)


# ОБНОВЛЕННАЯ функция рассылки - только участникам целевого чата[1]
async def start_broadcast(message: Message, text: str):
    """Запускает массовую рассылку сообщений только участникам целевого чата"""

    success_count = 0
    error_count = 0
    total_targets = 0

    # Прогресс сообщение
    progress_msg = await message.answer("🚀 **Запуск рассылки...**", parse_mode="Markdown")

    # Рассылка участникам целевого чата в личку
    if BROADCAST_SETTINGS['to_target_chat_members']:
        target_users = await get_target_chat_users()
        total_targets += len(target_users)

        await progress_msg.edit_text(
            f"📤 **Рассылка участникам целевого чата**\n"
            f"Отправляется {len(target_users)} участникам в личку...",
            parse_mode="Markdown"
        )

        for user_id in target_users:
            try:
                await bot.send_message(user_id, text, parse_mode="Markdown")
                success_count += 1
                await asyncio.sleep(0.05)  # Задержка для избежания лимитов
            except Exception as e:
                error_count += 1
                logging.warning(f"Не удалось отправить сообщение участнику целевого чата {user_id}: {e}")

    # Рассылка в целевой чат
    if BROADCAST_SETTINGS['to_target_chat']:
        total_targets += 1
        try:
            await bot.send_message(TARGET_CHAT_ID, text, parse_mode="Markdown")
            success_count += 1
        except Exception as e:
            error_count += 1
            logging.error(f"Не удалось отправить в целевой чат {TARGET_CHAT_ID}: {e}")

    # Рассылка в сетку чатов
    if BROADCAST_SETTINGS['to_network_chats']:
        await progress_msg.edit_text(
            f"🌐 **Рассылка в сетку чатов**\n"
            f"Подготовка списка чатов...",
            parse_mode="Markdown"
        )

        target_chats = []

        if BROADCAST_SETTINGS['network_chat_mode'] == "all":
            target_chats = list(BROADCAST_SETTINGS['available_chats'].keys())
        elif BROADCAST_SETTINGS['network_chat_mode'] == "members_only":
            # Отправляем только участникам целевого чата, которые есть в других чатах (в личку)
            all_members = set()
            for chat_id in BROADCAST_SETTINGS['available_chats'].keys():
                members = await get_chat_members(chat_id)
                all_members.update(members)

            # Фильтруем только участников целевого чата
            target_chat_members = set(await get_target_chat_users())
            filtered_members = all_members.intersection(target_chat_members)

            for user_id in filtered_members:
                try:
                    await bot.send_message(user_id, text, parse_mode="Markdown")
                    success_count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    error_count += 1
                    logging.warning(f"Не удалось отправить участнику {user_id}: {e}")

            total_targets += len(filtered_members)
        elif BROADCAST_SETTINGS['network_chat_mode'] == "specific_chats":
            target_chats = BROADCAST_SETTINGS['selected_chats']

        # Отправка в чаты
        if target_chats:
            total_targets += len(target_chats)

            await progress_msg.edit_text(
                f"🌐 **Рассылка в сетку чатов**\n"
                f"Отправляется в {len(target_chats)} чатов...",
                parse_mode="Markdown"
            )

            for chat_id in target_chats:
                try:
                    await bot.send_message(chat_id, text, parse_mode="Markdown")
                    success_count += 1
                    await asyncio.sleep(0.1)  # Задержка для чатов
                except Exception as e:
                    error_count += 1
                    logging.error(f"Не удалось отправить в чат {chat_id}: {e}")

    # Итоговый отчет
    report_text = (
        f"📊 **Рассылка завершена!**\n\n"
        f"✅ Успешно отправлено: {success_count}\n"
        f"❌ Ошибок: {error_count}\n"
        f"🎯 Всего целей: {total_targets}\n"
        f"📈 Успешность: {(success_count / total_targets * 100):.1f}%" if total_targets > 0 else "📈 Успешность: 0%"
                                                                                                f"\n\n🏷️ **Отправлено только участникам целевого чата**"
    )

    await progress_msg.edit_text(
        report_text,
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )


# Обработчики событий чата (без изменений)
@dp.message(F.content_type.in_({'new_chat_members'}))
async def new_member_handler(message: Message):
    """Отслеживание новых участников в чатах"""
    for user in message.new_chat_members:
        if not user.is_bot:
            await add_chat_member(user.id, message.chat.id, "member")
            await add_user(user.id, user.username, user.first_name, user.last_name, user.is_bot)

    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            members_count = await bot.get_chat_member_count(message.chat.id)
            await add_bot_chat(message.chat.id, message.chat.title, get_chat_type_string(message.chat.type), members_count)

        except Exception as e:
            logging.warning(f"Не удалось обновить информацию о чате {message.chat.id}: {e}")


@dp.message(F.content_type.in_({'left_chat_member'}))
async def left_member_handler(message: Message):
    """Отслеживание участников, покидающих чаты"""
    user = message.left_chat_member
    if not user.is_bot:
        await remove_chat_member(user.id, message.chat.id)
        logging.info(f"Пользователь {user.id} покинул чат {message.chat.id}")


@dp.message(F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]))
async def group_message_handler(message: Message):
    """Отслеживание активности в групповых чатах"""
    # Добавляем пользователя в базу
    user = message.from_user
    await add_user(user.id, user.username, user.first_name, user.last_name, user.is_bot)
    await add_chat_member(user.id, message.chat.id, "member")

    # Обновляем информацию о чате (с ограничением частоты)
    await update_chat_info_from_message(message)


@dp.message(Command("addchat"))
async def add_current_chat_handler(message: Message):
    """Добавляет текущий чат в список доступных (только для админа)"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для выполнения этой команды.")
        return

    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            await update_chat_info_from_message(message)
            await message.answer(
                f"✅ Чат '{message.chat.title}' добавлен в список доступных чатов!\n"
                f"🆔 ID чата: `{message.chat.id}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка при добавлении чата: {e}")
    else:
        await message.answer("❌ Эта команда работает только в групповых чатах.")



@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.clear()
    await message.answer(
        "❌ Операция отменена.",
        reply_markup=get_admin_keyboard()
    )


# Закрытие пула соединений при завершении
async def close_db():
    global db_pool
    if db_pool:
        await db_pool.close()


# Запуск бота
async def main():
    await init_db()
    await update_available_chats()
    logging.basicConfig(level=logging.INFO)

    print("🤖 Бот запущен!")
    print(f"🔐 Админ ID: {ADMIN_ID}")
    print(f"🎯 Целевой чат: {TARGET_CHAT_ID}")
    print(f"🗄️ База данных: {DB_CONFIG['database']}")
    print("ℹ️  Рассылка будет отправляться только участникам целевого чата!")

    try:
        await dp.start_polling(bot)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
