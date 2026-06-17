# ==================== منوی ربات ====================
class BotMenu:
    """مدیریت منوی ربات"""
    
    @staticmethod
    def get_main_menu() -> Dict:
        """منوی اصلی ربات"""
        keyboard = [
            [
                {'text': '🔮 جفرگیری', 'callback_data': 'jafr_start'},
                {'text': '📊 تاریخچه', 'callback_data': 'history'}
            ],
            [
                {'text': '📖 راهنما', 'callback_data': 'help'},
                {'text': '📈 آمار', 'callback_data': 'stats'}
            ],
            [
                {'text': 'ℹ️ درباره', 'callback_data': 'about'},
                {'text': '❌ لغو', 'callback_data': 'cancel'}
            ]
        ]
        return {
            'inline_keyboard': keyboard,
            'resize_keyboard': True
        }
    
    @staticmethod
    def get_jafr_menu() -> Dict:
        """منوی جفرگیری"""
        keyboard = [
            [
                {'text': '📝 جفر ۳۶', 'callback_data': 'jafr_36'},
                {'text': '📝 جفر ۳۶۰', 'callback_data': 'jafr_360'}
            ],
            [
                {'text': '🔮 هر دو جفر', 'callback_data': 'jafr_both'}
            ],
            [
                {'text': '🔙 بازگشت', 'callback_data': 'back_main'}
            ]
        ]
        return {'inline_keyboard': keyboard}
    
    @staticmethod
    def get_settings_menu() -> Dict:
        """منوی تنظیمات"""
        keyboard = [
            [
                {'text': '🔔 اعلان‌ها', 'callback_data': 'settings_notifications'},
                {'text': '🌙 حالت شب', 'callback_data': 'settings_darkmode'}
            ],
            [
                {'text': '🗑 پاک کردن تاریخچه', 'callback_data': 'settings_clear_history'}
            ],
            [
                {'text': '🔙 بازگشت', 'callback_data': 'back_main'}
            ]
        ]
        return {'inline_keyboard': keyboard}
    
    @staticmethod
    def get_quick_questions_menu() -> Dict:
        """سوالات سریع"""
        keyboard = [
            [
                {'text': '💼 موفقیت در کار', 'callback_data': 'quick_work'},
                {'text': '❤️ ازدواج و عشق', 'callback_data': 'quick_love'}
            ],
            [
                {'text': '💰 ثروت و پول', 'callback_data': 'quick_wealth'},
                {'text': '🏥 سلامتی', 'callback_data': 'quick_health'}
            ],
            [
                {'text': '🎓 تحصیلات', 'callback_data': 'quick_education'},
                {'text': '✈️ سفر', 'callback_data': 'quick_travel'}
            ],
            [
                {'text': '🔙 بازگشت', 'callback_data': 'back_main'}
            ]
        ]
        return {'inline_keyboard': keyboard}
    
    @staticmethod
    def get_confirmation_menu(question: str = "") -> Dict:
        """منوی تایید"""
        keyboard = [
            [
                {'text': '✅ تایید', 'callback_data': 'confirm_yes'},
                {'text': '❌ انصراف', 'callback_data': 'confirm_no'}
            ]
        ]
        return {'inline_keyboard': keyboard}

# ==================== Callback Handler ====================
class CallbackHandler:
    """مدیریت کلیک‌های روی دکمه‌ها"""
    
    @staticmethod
    def handle_callback(chat_id: str, callback_data: str) -> Optional[str]:
        """پردازش کلیک روی دکمه‌ها"""
        
        # ===== منوی اصلی =====
        if callback_data == 'back_main':
            TelegramBot.send_message(
                chat_id,
                "🔮 **منوی اصلی ربات جفر**\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                reply_markup=BotMenu.get_main_menu()
            )
            return None
        
        # ===== جفر =====
        elif callback_data == 'jafr_start':
            TelegramBot.send_message(
                chat_id,
                "🔮 **انتخاب نوع جفر**\n\nنوع جفر مورد نظر خود را انتخاب کنید:",
                reply_markup=BotMenu.get_jafr_menu()
            )
            return None
        
        elif callback_data == 'jafr_36':
            db.save_session(chat_id, 'name')
            TelegramBot.send_message(
                chat_id,
                "👤 **نام خود را وارد کنید:**\n\n(برای جفر ۳۶)"
            )
            return None
        
        elif callback_data == 'jafr_360':
            db.save_session(chat_id, 'name')
            TelegramBot.send_message(
                chat_id,
                "👤 **نام خود را وارد کنید:**\n\n(برای جفر ۳۶۰)"
            )
            return None
        
        elif callback_data == 'jafr_both':
            db.save_session(chat_id, 'name')
            TelegramBot.send_message(
                chat_id,
                "👤 **نام خود را وارد کنید:**\n\n(برای هر دو جفر ۳۶ و ۳۶۰)"
            )
            return None
        
        # ===== تاریخچه =====
        elif callback_data == 'history':
            return UserManager.get_history(chat_id)
        
        # ===== آمار =====
        elif callback_data == 'stats':
            return UserManager.get_stats(chat_id)
        
        # ===== راهنما =====
        elif callback_data == 'help':
            TelegramBot.send_message(
                chat_id,
                UserManager.get_help_message(),
                reply_markup=BotMenu.get_main_menu()
            )
            return None
        
        # ===== درباره =====
        elif callback_data == 'about':
            return """
ℹ️ **درباره ربات جفر**

🔮 **نسخه:** 2.0.0
📅 **تاریخ ایجاد:** 2026
👨‍💻 **توسعه‌دهنده:** تیم جفر

**ویژگی‌ها:**
• جفر ۳۶ و ۳۶۰
• تحلیل هوشمند سوالات
• تاریخچه سوالات
• آمار کاربری
• منوی تعاملی

⚠️ **توجه:** این ربات صرفاً جنبه سرگرمی دارد.
"""
        
        # ===== سوالات سریع =====
        elif callback_data.startswith('quick_'):
            question_type = callback_data.replace('quick_', '')
            quick_questions = {
                'work': "آیا در کارم موفق می‌شوم؟",
                'love': "آیا به زودی ازدواج می‌کنم؟",
                'wealth': "آیا ثروتمند می‌شوم؟",
                'health': "آیا سلامتی کامل دارم؟",
                'education': "آیا در تحصیل موفق می‌شوم؟",
                'travel': "آیا سفر خوبی خواهم داشت؟"
            }
            
            if question_type in quick_questions:
                question = quick_questions[question_type]
                # بررسی اینکه کاربر اطلاعاتش را وارد کرده یا نه
                session = db.get_session(chat_id)
                if session and session.get('name'):
                    # اگر کاربر قبلاً اطلاعاتش را وارد کرده
                    return UserManager.calculate_jafr(chat_id, question)
                else:
                    # شروع فرآیند جفر با سوال پیش‌فرض
                    db.save_session(chat_id, 'name')
                    TelegramBot.send_message(
                        chat_id,
                        f"👤 **لطفاً ابتدا نام خود را وارد کنید:**\n\nسوال شما: {question}"
                    )
                    # ذخیره سوال برای بعد
                    db.save_session(chat_id, 'name', question=question)
                    return None
        
        # ===== تایید =====
        elif callback_data == 'confirm_yes':
            return "✅ تایید شد! در حال پردازش..."
        
        elif callback_data == 'confirm_no':
            return "❌ عملیات لغو شد."
        
        # ===== لغو =====
        elif callback_data == 'cancel':
            return UserManager.cancel_session(chat_id)
        
        # ===== تنظیمات =====
        elif callback_data.startswith('settings_'):
            setting = callback_data.replace('settings_', '')
            settings_messages = {
                'notifications': "🔔 **تنظیمات اعلان‌ها**\n\nاعلان‌ها فعال هستند.",
                'darkmode': "🌙 **حالت شب**\n\nحالت شب فعال شد.",
                'clear_history': "🗑 **پاک کردن تاریخچه**\n\nآیا مطمئن هستید؟"
            }
            return settings_messages.get(setting, "تنظیمات ذخیره شد.")
        
        return "⚠️ گزینه نامعتبر است."

# ==================== به‌روزرسانی Webhook ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    """پردازش درخواست‌های وب‌هوک تلگرام"""
    try:
        update = request.get_json()
        if not update:
            return jsonify({'status': 'ok'}), 200
        
        # ===== پردازش پیام‌ها =====
        if 'message' in update:
            message = update['message']
            chat_id = str(message['chat']['id'])
            text = message.get('text', '').strip()
            
            # ثبت کاربر
            UserManager.register_user(update)
            
            # ===== دستورات =====
            if text.startswith('/'):
                if text == '/start':
                    TelegramBot.send_message(
                        chat_id,
                        "🔮 **ربات جفر ۳۶ و ۳۶۰**\n\nسلام! 👋\nمن یک ربات تخصصی در علم جفر هستم.\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                        reply_markup=BotMenu.get_main_menu()
                    )
                    return jsonify({'status': 'ok'}), 200
                
                elif text == '/menu':
                    TelegramBot.send_message(
                        chat_id,
                        "🔮 **منوی اصلی**\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                        reply_markup=BotMenu.get_main_menu()
                    )
                    return jsonify({'status': 'ok'}), 200
                
                elif text == '/ask':
                    db.save_session(chat_id, 'name')
                    TelegramBot.send_message(
                        chat_id,
                        "👤 **نام خود را وارد کنید:**",
                        reply_markup=BotMenu.get_main_menu()
                    )
                    return jsonify({'status': 'ok'}), 200
                
                else:
                    response = UserManager.process_command(chat_id, text)
                    if response:
                        TelegramBot.send_message(
                            chat_id,
                            response,
                            reply_markup=BotMenu.get_main_menu()
                        )
                    return jsonify({'status': 'ok'}), 200
            
            # ===== پردازش مراحل =====
            session = db.get_session(chat_id)
            if session:
                response, is_complete = UserManager.process_step(chat_id, text)
                if response:
                    # اگر مرحله سوال باشد و کاربر از منو استفاده کرده
                    if is_complete:
                        TelegramBot.send_message(
                            chat_id,
                            response,
                            reply_markup=BotMenu.get_main_menu()
                        )
                    else:
                        TelegramBot.send_message(chat_id, response)
                    return jsonify({'status': 'ok'}), 200
            
            # ===== دستور نامشخص =====
            TelegramBot.send_message(
                chat_id,
                "🤔 **دستور نامشخص**\n\nلطفاً از منو استفاده کنید یا /help را بزنید.",
                reply_markup=BotMenu.get_main_menu()
            )
        
        # ===== پردازش کلیک‌ها =====
        elif 'callback_query' in update:
            callback_query = update['callback_query']
            chat_id = str(callback_query['message']['chat']['id'])
            callback_data = callback_query['callback_data']
            
            # پاسخ به کلیک
            response = CallbackHandler.handle_callback(chat_id, callback_data)
            if response:
                TelegramBot.send_message(
                    chat_id,
                    response,
                    reply_markup=BotMenu.get_main_menu()
                )
            
            # تایید دریافت کلیک
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                    json={'callback_query_id': callback_query['id']}
                )
            except:
                pass
            
            return jsonify({'status': 'ok'}), 200
        
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        logger.error(f"خطا در وب‌هوک: {e}")
        return jsonify({'error': str(e)}), 500
