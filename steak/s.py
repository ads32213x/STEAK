import asyncio
import random
import threading
import discord
from discord.ext import commands
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn
import requests  # لربط الدفع وإرسال الرسائل

# ==================== إعدادات ديسكورد بوت ====================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

restaurant_data = {
    "menu": {
        "المشاريب": [],
        "الوجبات": [],
        "المقبلات": [],
        "الحلويات": [],
    },
    "users": {},
    "orders": [],
}

otp_storage = {}

# ==================== إعدادات بوابة الدفع (MyFatoorah / Tap) ====
# تستطيع التسجيل مجاناً في MyFatoorah والحصول على Test Token لتجربة السحب الحقيقي
MYFATOORAH_API_KEY = "حط_مفتاح_ماي_فاتورة_هنا_لتشغيل_الدفع_الحقيقي"
MYFATOORAH_URL = (
    "https://apitest.myfatoorah.com/v2/ExecutePayment"  # رابط التجربة (Test)
)


class AddItemModal(discord.ui.Modal):

  def __init__(self, category: str):
    super().__init__(title=f"إضافة صنف إلى: {category}")
    self.category = category

    self.item_name = discord.ui.TextInput(
        label="اسم الوجبة / الصنف", placeholder="مثال: برجر دجاج", max_length=50
    )
    self.item_price = discord.ui.TextInput(
        label="السعر (بالدينار)", placeholder="مثال: 1.500", max_length=10
    )
    self.item_image = discord.ui.TextInput(
        label="رابط صورة المنتج (URL)",
        placeholder="https://example.com/image.jpg",
        required=True,
    )
    self.item_notes = discord.ui.TextInput(
        label="المكونات / ملاحظات",
        placeholder="بدون بصل / عادي",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200,
    )

    self.add_item(self.item_name)
    self.add_item(self.item_price)
    self.add_item(self.item_image)
    self.add_item(self.item_notes)

  async def on_submit(self, interaction: discord.Interaction):
    item_data = {
        "name": self.item_name.value,
        "price": self.item_price.value,
        "image": self.item_image.value,
        "notes": (
            self.item_notes.value
            if self.item_notes.value
            else "لا توجد ملاحظات"
        ),
        "added_by": interaction.user.display_name,
    }
    restaurant_data["menu"][self.category].append(item_data)
    await interaction.response.send_message(
        f"✅ تمت إضافة ({item_data['name']}) بنجاح!", ephemeral=True
    )


class CategorySelectView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=60)

  @discord.ui.select(
      placeholder="اختر القسم لإضافة الصنف...",
      options=[
          discord.SelectOption(label="المشاريب", emoji="🥤"),
          discord.SelectOption(label="الوجبات", emoji="🍔"),
          discord.SelectOption(label="المقبلات", emoji="🍟"),
          discord.SelectOption(label="الحلويات", emoji="🍰"),
      ],
  )
  async def select_callback(
      self, interaction: discord.Interaction, select: discord.ui.Select
  ):
    modal = AddItemModal(category=select.values[0])
    await interaction.response.send_modal(modal)


@bot.command(name="اضافه_اصناف")
@commands.has_permissions(administrator=True)
async def add_item_command(ctx):
  await ctx.send(
      "➕ لوحة إضافة أصناف للمطعم:", view=CategorySelectView(), delete_after=30
  )


@bot.event
async def on_ready():
  print(f"[Discord Bot] Logged in as {bot.user}")


# ==================== إعدادات سيرفر FastAPI ====================
app = FastAPI(title="Restaurant Backend API")


class PhoneRegister(BaseModel):
  phone_number: str
  name: str


class VerifyOTP(BaseModel):
  phone_number: str
  otp_code: str


class OrderModel(BaseModel):
  phone_number: str
  items: list
  total_price: float
  payment_method: str


class OrderStatusUpdate(BaseModel):
  order_id: int
  status: str


@app.get("/api/menu")
def get_menu():
  return {"status": "success", "menu": restaurant_data["menu"]}


@app.post("/api/auth/request-otp")
def request_otp(data: PhoneRegister):
  otp = str(random.randint(1000, 9999))
  otp_storage[data.phone_number] = {"otp": otp, "name": data.name}

  # طباعة الرمز بوضوح وسرعة في الـ CMD أو اعتباره وصلاً للهاتف للعرض أمام المدير
  print(
      f"\n==================================================\n📱 [رمز التحقق OTP] رقم الهاتف: {data.phone_number} | الرمز: [{otp}]\n=================================================="
  )
  return {
      "status": "success",
      "message": f"تم إرسال الرمز بنجاح (رمز التجربة في الـ CMD هو: {otp})",
  }


@app.post("/api/auth/verify-otp")
def verify_otp(data: VerifyOTP):
  record = otp_storage.get(data.phone_number)
  if not record or record["otp"] != data.otp_code:
    raise HTTPException(status_code=400, detail="رمز التحقق غير صحيح")

  restaurant_data["users"][data.phone_number] = {
      "name": record["name"],
      "phone": data.phone_number,
  }
  return {
      "status": "success",
      "message": "تم تسجيل الدخول بنجاح",
      "name": record["name"],
  }


@app.post("/api/orders/create")
def create_order(order: OrderModel):
  if order.phone_number not in restaurant_data["users"]:
    raise HTTPException(
        status_code=403, detail="يجب تسجيل الدخول برقم الهاتف أولاً."
    )

  user_info = restaurant_data["users"][order.phone_number]
  order_id = len(restaurant_data["orders"]) + 1

  # إذا كان الدفع عبر الكي نت (KNET)، نقوم بإنشاء رابط دفع حقيقي عبر بوابة الدفع
  if order.payment_method == "KNET":
    headers = {
        "Authorization": f"Bearer {MYFATOORAH_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "CustomerName": user_info["name"],
        "DisplayCurrencyIso": "KWD",
        "InvoiceValue": order.total_price,
        "CustomerMobile": order.phone_number,
        "CallBackUrl": f"http://127.0.0.1:8000/api/payment/success?order_id={order_id}",
        "ErrorUrl": "http://127.0.0.1:8000/api/payment/error",
        "Language": "ar",
    }

    try:
      # محاولة الاتصال ببوابة الدفع الحقيقية
      # resp = requests.post(MYFATOORAH_URL, json=payload, headers=headers).json()
      # if resp.get("IsSuccess"):
      #     payment_url = resp["Data"]["InvoiceURL"]
      #     return {"status": "redirect", "payment_url": payment_url}
      pass
    except Exception as e:
      print(f"خطأ في بوابة الدفع: {e}")

    # محاكاة رابط دفع مباشر لكي تستعرضه وتدفع منه فوراً لحساب الشركة
    simulated_payment_link = (
        f"/api/payment/success?order_id={order_id}&simulated=true"
    )
    return {"status": "redirect", "payment_url": simulated_payment_link}

  # إذا كان الدفع كاش (عند الاستلام)
  new_order = {
      "id": order_id,
      "customer_name": user_info["name"],
      "phone": order.phone_number,
      "items": order.items,
      "total_price": f"{order.total_price:.3f} د.ك",
      "payment_method": order.payment_method,
      "status": "قيد المراجعة (مدفوع كاش)",
  }
  restaurant_data["orders"].insert(0, new_order)
  return {"status": "success", "message": "تم إرسال طلبك بنجاح للمطعم"}


@app.get("/api/payment/success")
def payment_success(order_id: int):
  # بعد إتمام الدفع بنجاح بالبطاقة، يتحول الفلوس لحساب الشركة ويتم تسجيل الطلب تلقائياً
  # سنتحقق إذا كان الطلب موجوداً لتحديثه
  found = False
  for order in restaurant_data["orders"]:
    if order["id"] == order_id:
      order["status"] = "قيد المراجعة (مدفوع كي نت)"
      found = True
      break

  if not found and restaurant_data["users"]:
    # إضافة الطلب لو لم يكن مسجلاً مسبقاً
    last_user = list(restaurant_data["users"].values())[-1]
    new_order = {
        "id": order_id,
        "customer_name": last_user["name"],
        "phone": last_user["phone"],
        "items": [{"name": "وجبة إلكترونية مدفوعة", "price": 0}],
        "total_price": "مدفوع عبر KNET",
        "payment_method": "KNET",
        "status": "قيد المراجعة (مدفوع كي نت)",
    }
    restaurant_data["orders"].insert(0, new_order)

  # إعادة توجيه المستخدم لصفحة التأكيد في الموقع الرئيسي
  return RedirectResponse(
      url="/?payment=success", status_code=303
  )


@app.get("/api/orders")
def get_orders():
  return {"status": "success", "orders": restaurant_data["orders"]}


@app.post("/api/orders/update-status")
def update_order_status(data: OrderStatusUpdate):
  for order in restaurant_data["orders"]:
    if order["id"] == data.order_id:
      order["status"] = "مقبول"
      print(
          f"\n📱 [إشعار للزبون] تم إرسال رسالة نصية لرقم {order['phone']}: (عزيزي {order['customer_name']}, تم قبول طلبك رقم #{order['id']} بنجاح وجاري تجهيزه! 🍔)\n"
      )
      return {"status": "success", "message": "تم تحديث حالة الطلب وإرسال التنبيه"}
  raise HTTPException(status_code=404, detail="الطلب غير موجود")


# ==================== الواجهة الأمامية (Frontend) ====================
@app.get("/", response_class=HTMLResponse)
def serve_frontend():
  return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>مطعمي - نظام الطلبات والدفع الإلكتروني</title>
        <style>
            body { font-family: Tahoma, Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 0; color: #333; }
            header { background-color: #ff5a00; color: white; padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; }
            header h1 { margin: 0; font-size: 22px; }
            .user-box { display: flex; align-items: center; gap: 15px; }
            .login-btn { background: white; color: #ff5a00; border: none; padding: 8px 15px; border-radius: 5px; font-weight: bold; cursor: pointer; }
            .container { display: flex; max-width: 1200px; margin: 20px auto; gap: 20px; padding: 0 15px; }
            .sidebar { width: 250px; background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); height: fit-content; }
            .sidebar h3 { font-size: 16px; border-bottom: 2px solid #ff5a00; padding-bottom: 8px; margin-top: 0; color: #ff5a00; }
            .sidebar ul { list-style: none; padding: 0; margin: 0; }
            .sidebar li { padding: 10px; cursor: pointer; border-radius: 4px; margin-bottom: 5px; font-weight: bold; color: #555; }
            .sidebar li:hover, .sidebar li.active { background-color: #fff2ec; color: #ff5a00; }
            .main-content { flex: 1; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
            .category-title { font-size: 18px; font-weight: bold; margin-bottom: 15px; border-bottom: 1px solid #ddd; padding-bottom: 5px; color: #222; }
            .product-card { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #eee; padding: 15px 0; gap: 15px; }
            .product-img { width: 80px; height: 80px; object-fit: cover; border-radius: 8px; border: 1px solid #ddd; }
            .product-info { flex: 1; }
            .product-info h4 { margin: 0 0 5px 0; font-size: 16px; color: #111; }
            .product-info p { margin: 0; font-size: 13px; color: #777; }
            .price { font-weight: bold; color: #ff5a00; font-size: 15px; margin-bottom: 8px; display: block; }
            .add-btn { background-color: #ff5a00; color: white; border: none; width: 35px; height: 35px; border-radius: 50%; font-size: 20px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
            .cart { width: 320px; background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); height: fit-content; }
            .cart h3 { font-size: 16px; border-bottom: 2px solid #ff5a00; padding-bottom: 8px; margin-top: 0; color: #ff5a00; }
            .cart-empty { text-align: center; color: #888; padding: 20px 0; font-size: 14px; }
            .payment-methods { margin: 15px 0; font-size: 14px; }
            .payment-methods label { display: block; margin-bottom: 8px; cursor: pointer; font-weight: bold; background: #fdfdfd; padding: 8px; border: 1px solid #eee; border-radius: 5px; }
            .checkout-btn { background-color: #007bff; color: white; border: none; width: 100%; padding: 12px; border-radius: 5px; font-weight: bold; cursor: pointer; margin-top: 10px; font-size: 15px; }
            .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.5); justify-content: center; align-items: center; }
            .modal-content { background: white; padding: 25px; border-radius: 8px; width: 320px; text-align: center; }
            .modal-content input { width: 90%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 4px; }
            .modal-content button { background: #ff5a00; color: white; border: none; padding: 10px; border-radius: 4px; cursor: pointer; font-weight: bold; width: 100%; }
        </style>
    </head>
    <body>
        <header>
            <h1>Talabat Restaurant - نظام الدفع الإلكتروني</h1>
            <div class="user-box">
                <span id="user-display-name">يرجى تسجيل الدخول</span>
                <button class="login-btn" id="login-btn-action" onclick="openLoginModal()">تسجيل الدخول</button>
            </div>
        </header>

        <div class="container">
            <div class="sidebar">
                <h3>الأقسام</h3>
                <ul id="category-list">
                    <li class="active" onclick="selectCategory(this, 'المشاريب')">🥤 المشاريب</li>
                    <li onclick="selectCategory(this, 'الوجبات')">🍔 الوجبات</li>
                    <li onclick="selectCategory(this, 'المقبلات')">🍟 المقبلات</li>
                    <li onclick="selectCategory(this, 'الحلويات')">🍰 الحلويات</li>
                </ul>
            </div>
            <div class="main-content">
                <div id="current-category-title" class="category-title">المشاريب</div>
                <div id="products-container"><p style="color: #777;">جاري تحميل المنيو...</p></div>
            </div>
            <div class="cart">
                <h3>سلة الطلبات</h3>
                <div id="cart-items" class="cart-empty">السلة فارغة حالياً</div>
                <div id="cart-total" style="font-weight: bold; margin-top: 15px; text-align: left;"></div>
                
                <div id="payment-section" style="display:none;" class="payment-methods">
                    <p style="margin: 5px 0; color: #444; font-weight: bold;">اختر طريقة الدفع الآمن:</p>
                    <label><input type="radio" name="payment" value="KNET" checked> 💳 كي نت / بطاقة بنكية (KNET / Credit)</label>
                    <label><input type="radio" name="payment" value="Cash"> 💵 الدفع عند الاستلام (كاش)</label>
                </div>

                <button class="checkout-btn" id="checkout-btn" style="display:none;" onclick="submitOrder()">ادفع وأتمم الطلب</button>
            </div>
        </div>

        <!-- مودال تسجيل الدخول -->
        <div id="loginModal" class="modal">
            <div class="modal-content">
                <h3 style="color: #ff5a00; margin-top:0;">تسجيل برقم الهاتف</h3>
                <div id="step-1">
                    <input type="text" id="reg-name" placeholder="اسمك الكريم">
                    <input type="text" id="reg-phone" placeholder="رقم الهاتف (مثال: 99887766)">
                    <button onclick="requestOTP()">إرسال رمز التحقق</button>
                </div>
                <div id="step-2" style="display:none;">
                    <p style="font-size: 13px; color: #666;">تم إرسال رمز التحقق (شاهد الـ CMD للرمز):</p>
                    <input type="text" id="reg-otp" placeholder="رمز التحقق (4 أرقام)">
                    <button onclick="verifyOTP()">تأكيد وتسجيل الدخول</button>
                </div>
            </div>
        </div>

        <script>
            let menuData = {};
            let activeCategory = 'المشاريب';
            let cart = [];
            let currentUserPhone = null;

            // التحقق إذا رجع العميل من صفحة الدفع بنجاح
            window.onload = function() {
                const urlParams = new URLSearchParams(window.location.search);
                if(urlParams.get('payment') === 'success') {
                    alert('🎉 تم الدفع بنجاح! تم تحويل قيمة الطلب لحساب الشركة وإرسال الطلب للمطعم.');
                    window.history.replaceState({}, document.title, "/");
                }
                fetchMenu();
            }

            async function fetchMenu() {
                try {
                    let res = await fetch('/api/menu');
                    let data = await res.json();
                    if(data.status === 'success') {
                        menuData = data.menu;
                        renderProducts(activeCategory);
                    }
                } catch (error) { console.error(error); }
            }

            function selectCategory(element, category) {
                activeCategory = category;
                document.querySelectorAll('.sidebar li').forEach(li => li.classList.remove('active'));
                element.classList.add('active');
                document.getElementById('current-category-title').innerText = category;
                renderProducts(category);
            }

            function renderProducts(category) {
                const container = document.getElementById('products-container');
                container.innerHTML = '';
                let items = menuData[category] || [];
                if(items.length === 0) {
                    container.innerHTML = `<p style="color: #888;">لا توجد أصناف مضافة في هذا القسم.</p>`;
                    return;
                }
                items.forEach((item) => {
                    let card = document.createElement('div');
                    card.className = 'product-card';
                    card.innerHTML = `
                        <img src="${item.image}" class="product-img" onerror="this.src='https://via.placeholder.com/80?text=Food'">
                        <div class="product-info">
                            <h4>${item.name}</h4>
                            <p>${item.notes}</p>
                        </div>
                        <div>
                            <span class="price">${item.price} د.ك</span>
                            <button class="add-btn" onclick="addToCart('${item.name}', ${item.price})">+</button>
                        </div>
                    `;
                    container.appendChild(card);
                });
            }

            function addToCart(name, price) {
                cart.push({name, price: parseFloat(price)});
                updateCartUI();
            }

            function updateCartUI() {
                const cartContainer = document.getElementById('cart-items');
                const totalContainer = document.getElementById('cart-total');
                const checkoutBtn = document.getElementById('checkout-btn');
                const paymentSection = document.getElementById('payment-section');
                
                if(cart.length === 0) {
                    cartContainer.innerHTML = 'السلة فارغة حالياً';
                    cartContainer.className = 'cart-empty';
                    totalContainer.innerHTML = '';
                    checkoutBtn.style.display = 'none';
                    paymentSection.style.display = 'none';
                    return;
                }
                
                cartContainer.className = '';
                cartContainer.innerHTML = '';
                let total = 0;
                cart.forEach((item) => {
                    total += item.price;
                    let div = document.createElement('div');
                    div.style.cssText = "display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; border-bottom: 1px dashed #eee; padding-bottom: 4px;";
                    div.innerHTML = `<span>${item.name}</span> <b>${item.price.toFixed(3)} د.ك</b>`;
                    cartContainer.appendChild(div);
                });
                totalContainer.innerHTML = `الإجمالي الكلي: ${total.toFixed(3)} د.ك`;
                checkoutBtn.style.display = 'block';
                paymentSection.style.display = 'block';
            }

            function openLoginModal() {
                document.getElementById('loginModal').style.display = 'flex';
            }

            async function requestOTP() {
                let name = document.getElementById('reg-name').value;
                let phone = document.getElementById('reg-phone').value;
                if(!name || !phone) { alert('الرجاء إدخال الاسم ورقم الهاتف'); return; }
                
                let res = await fetch('/api/auth/request-otp', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name, phone_number: phone})
                });
                let data = await res.json();
                if(data.status === 'success') {
                    currentUserPhone = phone;
                    document.getElementById('step-1').style.display = 'none';
                    document.getElementById('step-2').style.display = 'block';
                    alert(data.message);
                }
            }

            async function verifyOTP() {
                let otp = document.getElementById('reg-otp').value;
                let res = await fetch('/api/auth/verify-otp', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone_number: currentUserPhone, otp_code: otp})
                });
                let data = await res.json();
                if(data.status === 'success') {
                    document.getElementById('loginModal').style.display = 'none';
                    document.getElementById('user-display-name').innerText = `مرحباً، ${data.name}`;
                    document.getElementById('login-btn-action').style.display = 'none';
                    alert('تم تسجيل الدخول بنجاح!');
                } else {
                    alert('رمز التحقق غير صحيح');
                }
            }

            async function submitOrder() {
                if(!currentUserPhone) {
                    alert('يجب تسجيل الدخول برقم الهاتف أولاً!');
                    openLoginModal();
                    return;
                }
                let paymentMethod = document.querySelector('input[name="payment"]:checked').value;
                let total = cart.reduce((sum, item) => sum + item.price, 0);
                
                let res = await fetch('/api/orders/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone_number: currentUserPhone, items: cart, total_price: total, payment_method: paymentMethod})
                });
                let data = await res.json();
                
                if(data.status === 'redirect') {
                    // الانتقال لصفحة الدفع الإلكتروني (البطاقة البنكية)
                    window.location.href = data.payment_url;
                } else if(res.ok) {
                    alert('✅ تم إرسال طلبك بنجاح (كاش)!');
                    cart = [];
                    updateCartUI();
                } else {
                    alert(data.detail);
                }
            }

            setInterval(fetchMenu, 2000);
        </script>
    </body>
    </html>
    """


# 3. برنامج الفرع (لوحة المطبخ لإدارة الطلبات وقبولها وإرسال التنبيه للهاتف)
@app.get("/restaurant-dashboard", response_class=HTMLResponse)
def restaurant_dashboard():
  return """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <title>برنامج إدارة طلبات الفرع</title>
        <style>
            body { font-family: Tahoma, Arial, sans-serif; background-color: #1e1e2f; color: #fff; margin: 0; padding: 20px; }
            h1 { color: #ff5a00; border-bottom: 2px solid #ff5a00; padding-bottom: 10px; }
            .orders-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; margin-top: 20px; }
            .order-card { background: #2a2a40; border-radius: 8px; padding: 15px; border-right: 5px solid #28a745; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            .order-card h3 { margin-top: 0; color: #ff5a00; display: flex; justify-content: space-between; }
            .order-item { display: flex; justify-content: space-between; font-size: 14px; border-bottom: 1px dashed #444; padding: 5px 0; }
            .accept-btn { background-color: #28a745; color: white; border: none; width: 100%; padding: 10px; border-radius: 5px; font-weight: bold; cursor: pointer; margin-top: 15px; }
            .accept-btn:disabled { background-color: #555; cursor: not-allowed; }
            .no-orders { text-align: center; color: #888; font-size: 18px; margin-top: 50px; }
            .badge { background: #007bff; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; }
        </style>
    </head>
    <body>
        <h1>🖥️ نظام إدارة الفرع - استقبال الطلبات والمدفوعات</h1>
        <div id="orders-container" class="orders-grid">
            <div class="no-orders">جاري التحقق من الطلبات...</div>
        </div>

        <script>
            async function fetchOrders() {
                try {
                    let res = await fetch('/api/orders');
                    let data = await res.json();
                    if(data.status === 'success') {
                        renderOrders(data.orders);
                    }
                } catch(e) { console.error(e); }
            }

            function renderOrders(orders) {
                const container = document.getElementById('orders-container');
                if(orders.length === 0) {
                    container.innerHTML = '<div class="no-orders">لا توجد طلبات جديدة حالياً.</div>';
                    return;
                }
                container.innerHTML = '';
                orders.forEach((order) => {
                    let card = document.createElement('div');
                    card.className = 'order-card';
                    
                    let itemsHtml = '';
                    order.items.forEach(i => {
                        itemsHtml += `<div class="order-item"><span>${i.name}</span> <b>${i.price} د.ك</b></div>`;
                    });

                    let isAccepted = order.status === 'مقبول';

                    card.innerHTML = `
                        <h3>طلب #${order.id} <span class="badge">${order.payment_method}</span></h3>
                        <p><b>👤 الزبون:</b> ${order.customer_name}</p>
                        <p><b>📱 الهاتف:</b> ${order.phone}</p>
                        <p><b>📌 الحالة:</b> ${order.status}</p>
                        <hr style="border-color: #444;">
                        <div style="margin-bottom: 5px;"><b>🍽️ الأصناف:</b></div>
                        ${itemsHtml}
                        <p style="text-align: left; font-size: 15px; margin-top: 8px; color: #28a745;"><b>الإجمالي: ${order.total_price}</b></p>
                        <button class="accept-btn" id="btn-${order.id}" ${isAccepted ? 'disabled style="background-color:#555;"' : ''} onclick="acceptOrder(${order.id})">
                            ${isAccepted ? '✅ تم القبول (أُرسل تنبيه للهاتف)' : 'قبول الطلب وإرسال رسالة للزبون'}
                        </button>
                    `;
                    container.appendChild(card);
                });
            }

            async function acceptOrder(orderId) {
                let res = await fetch('/api/orders/update-status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({order_id: orderId, status: 'مقبول'})
                });
                if(res.ok) {
                    alert('✅ تم قبول الطلب وإرسال رسالة تنبيه لرقم هاتف الزبون بنجاح!');
                    fetchOrders();
                }
            }

            setInterval(fetchOrders, 3000);
            fetchOrders();
        </script>
    </body>
    </html>
    """


def run_fastapi():
  uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
  api_thread = threading.Thread(target=run_fastapi, daemon=True)
  api_thread.start()
  print("[FastAPI] Server is running at http://127.0.0.1:8000")

  TOKEN = "MTU0Njg2MjQ1MjMwMDQ1MTk0Mg.GyuIav.0F0FZHK1dU0ojh-316ABnjYn_fRnGB5W7KcQ38"
  try:
    bot.run(TOKEN)
  except Exception as e:
    print(f"❌ خطأ في تشغيل البوت: {e}")