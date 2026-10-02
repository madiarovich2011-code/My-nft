import asyncio, os, sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PAY_INFO = os.environ.get("PAY_INFO", "Реквизиты для оплаты: задай PAY_INFO в Variables")
SUPPORT = os.environ.get("SUPPORT", "@username")
STAR_PRICE = float(os.environ.get("STAR_PRICE", "1.4"))  # рублей за 1 звезду
PREMIUM = {3: 1200, 6: 1600, 12: 2800}  # месяцев: цена в рублях
REF_PERCENT = 10
STARS_RATE = float(os.environ.get("STARS_RATE", "0.6"))  # сколько ⭐ берём за 1 рубль цены

db = sqlite3.connect(os.environ.get("DB_PATH", "bot.db"))
db.execute("create table if not exists users(id integer primary key, name text, ref integer, balance real default 0)")
db.execute("create table if not exists orders(id integer primary key autoincrement, user_id integer, item text, price real, status text default 'new')")
db.commit()

bot = Bot(TOKEN)
dp = Dispatcher()


class S(StatesGroup):
    amount = State()
    username = State()
    broadcast = State()


def kb(rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[types.InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


MENU = kb([
    [("⭐ Купить звёзды", "stars")],
    [("🎁 Premium", "premium")],
    [("📦 Мои заказы", "orders")],
    [("👛 Профиль", "profile"), ("🤝 Рефералка", "ref")],
    [("🛟 Поддержка", "support")],
])


@dp.message(CommandStart())
async def start(m: types.Message, command: CommandObject):
    ref = int(command.args) if command.args and command.args.isdigit() else None
    if not db.execute("select 1 from users where id=?", (m.from_user.id,)).fetchone():
        if ref == m.from_user.id:
            ref = None
        db.execute("insert into users(id,name,ref) values(?,?,?)", (m.from_user.id, m.from_user.full_name, ref))
        db.commit()
    await m.answer(
        f"Привет, {m.from_user.first_name}!\n\n"
        "Здесь можно купить Telegram Stars и Premium дешевле.\n"
        "Приводи друзей и получай процент с их заказов.",
        reply_markup=MENU)


@dp.callback_query(F.data == "stars")
async def stars(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(S.amount)
    await state.update_data(kind="stars")
    await c.message.answer(f"Сколько звёзд нужно? (от 50)\nЦена: {STAR_PRICE} ₽ за звезду")
    await c.answer()


@dp.callback_query(F.data == "premium")
async def premium(c: types.CallbackQuery):
    rows = [[(f"{mo} мес. — {p} ₽", f"prem:{mo}")] for mo, p in PREMIUM.items()]
    await c.message.answer("Выбери срок:", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("prem:"))
async def prem_pick(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(S.username)
    await state.update_data(kind="premium", months=int(c.data.split(":")[1]))
    await c.message.answer("Напиши @username, кому подарить Premium (или свой)")
    await c.answer()


@dp.message(S.amount)
async def get_amount(m: types.Message, state: FSMContext):
    if not (m.text or "").isdigit() or int(m.text) < 50:
        return await m.answer("Введи число от 50")
    await state.update_data(amount=int(m.text))
    await state.set_state(S.username)
    await m.answer("Напиши @username получателя (или свой)")


@dp.message(S.username)
async def get_username(m: types.Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    target = (m.text or "").strip()
    if d["kind"] == "stars":
        item, price = f"{d['amount']}⭐ для {target}", round(d["amount"] * STAR_PRICE, 2)
    else:
        item, price = f"Premium {d['months']} мес. для {target}", PREMIUM[d["months"]]
    cur = db.execute("insert into orders(user_id,item,price) values(?,?,?)", (m.from_user.id, item, price))
    db.commit()
    oid = cur.lastrowid
    await m.answer(
        f"Заказ #{oid}\n{item}\nК оплате: {price} ₽\n\n{PAY_INFO}\n\nПосле оплаты нажми кнопку.",
        reply_markup=kb([[("⭐ Оплатить звёздами", f"pay:{oid}")],
                         [("✅ Я оплатил (перевод)", f"paid:{oid}")]]))


@dp.callback_query(F.data.startswith("paid:"))
async def paid(c: types.CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = db.execute("select user_id,item,price,status from orders where id=?", (oid,)).fetchone()
    if not o or o[0] != c.from_user.id or o[3] != "new":
        return await c.answer("Заказ недоступен", show_alert=True)
    db.execute("update orders set status='paid' where id=?", (oid,))
    db.commit()
    for a in ADMINS:
        await bot.send_message(
            a, f"💰 Оплата #{oid}\n{o[1]}\n{o[2]} ₽\nОт: {c.from_user.full_name} ({c.from_user.id})",
            reply_markup=kb([[("✅ Выдано", f"ok:{oid}"), ("❌ Отклонить", f"no:{oid}")]]))
    await c.message.answer("Ждём подтверждения от админа.")
    await c.answer()


@dp.callback_query(F.data.startswith("pay:"))
async def pay_stars(c: types.CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = db.execute("select user_id,item,price,status from orders where id=?", (oid,)).fetchone()
    if not o or o[0] != c.from_user.id or o[3] != "new":
        return await c.answer("Заказ недоступен", show_alert=True)
    amount = max(1, round(o[2] * STARS_RATE))
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=f"Заказ #{oid}",
        description=o[1],
        payload=str(oid),
        currency="XTR",
        provider_token="",
        prices=[types.LabeledPrice(label=f"Заказ #{oid}", amount=amount)])
    await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    o = db.execute("select status from orders where id=?", (int(q.invoice_payload),)).fetchone()
    if o and o[0] == "new":
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Заказ уже оплачен или не найден")


@dp.message(F.successful_payment)
async def got_payment(m: types.Message):
    oid = int(m.successful_payment.invoice_payload)
    db.execute("update orders set status='paid' where id=? and status='new'", (oid,))
    db.commit()
    o = db.execute("select item,price from orders where id=?", (oid,)).fetchone()
    for a in ADMINS:
        await bot.send_message(
            a, f"⭐ Оплачено звёздами #{oid}\n{o[0]}\n{m.successful_payment.total_amount} ⭐\n"
               f"От: {m.from_user.full_name} ({m.from_user.id})",
            reply_markup=kb([[("✅ Выдано", f"ok:{oid}"), ("❌ Отклонить", f"no:{oid}")]]))
    await m.answer("Оплата получена ✅ Ждём выдачу от админа.")


@dp.callback_query(F.data == "orders")
async def my_orders(c: types.CallbackQuery):
    rows = db.execute("select id,item,price,status from orders where user_id=? order by id desc limit 10", (c.from_user.id,)).fetchall()
    text = "\n".join(f"#{r[0]} {r[1]} — {r[2]} ₽ [{r[3]}]" for r in rows) or "Заказов пока нет"
    await c.message.answer(text)
    await c.answer()


@dp.callback_query(F.data == "profile")
async def profile(c: types.CallbackQuery):
    bal = db.execute("select balance from users where id=?", (c.from_user.id,)).fetchone()
    n = db.execute("select count(*) from orders where user_id=? and status='done'", (c.from_user.id,)).fetchone()[0]
    await c.message.answer(f"ID: {c.from_user.id}\nБаланс: {bal[0] if bal else 0} ₽\nВыполненных заказов: {n}")
    await c.answer()


@dp.callback_query(F.data == "ref")
async def ref(c: types.CallbackQuery):
    me = await bot.get_me()
    cnt = db.execute("select count(*) from users where ref=?", (c.from_user.id,)).fetchone()[0]
    await c.message.answer(f"Твоя ссылка:\nhttps://t.me/{me.username}?start={c.from_user.id}\n\n"
                           f"Приглашено: {cnt}\nТы получаешь {REF_PERCENT}% с заказов друзей.")
    await c.answer()


@dp.callback_query(F.data == "support")
async def support(c: types.CallbackQuery):
    await c.message.answer(f"Поддержка: {SUPPORT}")
    await c.answer()


# ---------- АДМИН ----------
def is_admin(uid): return uid in ADMINS


@dp.message(Command("admin"))
async def admin(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("🛠 Админ-панель", reply_markup=kb([
        [("📊 Статистика", "a_stats")],
        [("🧾 Ожидают выдачи", "a_pending")],
        [("📢 Рассылка", "a_bc")],
    ]))


@dp.callback_query(F.data == "a_stats")
async def a_stats(c: types.CallbackQuery):
    if not is_admin(c.from_user.id): return
    u = db.execute("select count(*) from users").fetchone()[0]
    o = db.execute("select count(*), coalesce(sum(price),0) from orders where status='done'").fetchone()
    await c.message.answer(f"Пользователей: {u}\nВыполнено заказов: {o[0]}\nОборот: {o[1]} ₽")
    await c.answer()


@dp.callback_query(F.data == "a_pending")
async def a_pending(c: types.CallbackQuery):
    if not is_admin(c.from_user.id): return
    rows = db.execute("select id,item,price,user_id from orders where status='paid'").fetchall()
    if not rows:
        await c.message.answer("Пусто")
    for r in rows:
        await c.message.answer(f"#{r[0]} {r[1]} — {r[2]} ₽ (user {r[3]})",
                               reply_markup=kb([[("✅ Выдано", f"ok:{r[0]}"), ("❌ Отклонить", f"no:{r[0]}")]]))
    await c.answer()


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def decide(c: types.CallbackQuery):
    if not is_admin(c.from_user.id): return
    act, oid = c.data.split(":")
    o = db.execute("select user_id,price,status from orders where id=?", (int(oid),)).fetchone()
    if not o or o[2] != "paid":
        return await c.answer("Уже обработан", show_alert=True)
    if act == "ok":
        db.execute("update orders set status='done' where id=?", (oid,))
        r = db.execute("select ref from users where id=?", (o[0],)).fetchone()
        if r and r[0]:
            db.execute("update users set balance=balance+? where id=?", (o[1] * REF_PERCENT / 100, r[0]))
        await bot.send_message(o[0], f"✅ Заказ #{oid} выполнен!")
    else:
        db.execute("update orders set status='rejected' where id=?", (oid,))
        await bot.send_message(o[0], f"❌ Заказ #{oid} отклонён. Напиши в поддержку: {SUPPORT}")
    db.commit()
    await c.message.edit_text(c.message.text + f"\n\n→ {'выдано' if act == 'ok' else 'отклонено'}")
    await c.answer()


@dp.callback_query(F.data == "a_bc")
async def a_bc(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return
    await state.set_state(S.broadcast)
    await c.message.answer("Пришли текст рассылки")
    await c.answer()


@dp.message(S.broadcast)
async def do_bc(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    ok = 0
    for (uid,) in db.execute("select id from users").fetchall():
        try:
            await bot.send_message(uid, m.text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"Отправлено: {ok}")


async def main():
    await dp.start_polling(bot)

asyncio.run(main())
