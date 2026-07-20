from playwright.sync_api import sync_playwright
import sys
import time
import re
from bidi.algorithm import get_display

# הגדרת עברית לטרמינל
sys.stdout.reconfigure(encoding='utf-8')

def print_heb(text):
    """פונקציית עזר להדפסת עברית תקינה בטרמינל"""
    print(get_display(text))

def is_foreign_asset(term):
    """בודק אם יש אותיות באנגלית - מסמן שזה נייר מחו"ל"""
    return bool(re.search(r'[a-zA-Z]', term))

def finance_engine_globes(search_term):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        # Globes now shows a "Funding Choices" cookie-consent overlay
        # (fc-consent-root) that intercepts clicks on the search icon and the
        # results row further down, causing every click to retry for 60s
        # before timing out. It's injected asynchronously (a fixed-delay
        # removal right after page load raced it and sometimes lost), and
        # clicking its "consent" button was selector-fragile (text/markup
        # varies) — so instead, run a MutationObserver from the moment the
        # page starts loading that strips the overlay the instant it appears,
        # for the whole page lifetime. Removes the race entirely.
        context.add_init_script("""
            (function() {
                function stripConsent() {
                    document.querySelectorAll('.fc-consent-root').forEach(el => el.remove());
                }
                new MutationObserver(stripConsent).observe(document.documentElement, {childList: true, subtree: true});
                stripConsent();
            })();
        """)
        page = context.new_page()
        page.set_default_timeout(60000)

        try:
            print_heb(f"🌐 מתחבר לגלובס ומחפש: {search_term}...")
            page.goto('https://www.globes.co.il/portal/', wait_until='domcontentloaded')
            page.wait_for_selector('.navWmainI.search')
            # force=True: the consent overlay removal above doesn't reliably
            # win the race against Globes' own script re-inserting it, and
            # debugging exactly why is a time sink for what's a fallback path
            # anyway — force bypasses Playwright's "is this element actually
            # clickable" check entirely, clicking regardless of what's
            # visually on top of it.
            page.click('.navWmainI.search', force=True)

            search_input = page.locator('#query_for_site')
            search_input.click(force=True)
            search_input.type(search_term, delay=200)
            
            print_heb("⏳ מנתח את התוצאות...")
            page.wait_for_selector('.C_divHiddenSearch.opened', timeout=15000)
            
            results = page.locator('.C_divHiddenSearch.opened tr').all()
            
            selected_res = None
            asset_name = ""
            target_symbol = search_term.upper()
            
            for res in results:
                text = res.inner_text().strip()
                if not text or "שם נייר" in text or 'סוג ני"ע' in text:
                    continue
                
                words_in_row = text.upper().split()
                
                if target_symbol in words_in_row:
                    selected_res = res
                    asset_name = text.replace('\n', ' | ')
                    break

            if not selected_res:
                print_heb("❌ לא מצאתי תוצאות מתאימות לחיפוש עם התאמה מדויקת.")
                return None, None, None, None

            print_heb(f"✅ מיידע: הבוט זיהה ובחר אוטומטית בנייר:\n   [{asset_name}]")
            selected_res.click(force=True)
            
            time.sleep(3)
            page.wait_for_load_state('domcontentloaded')

            print_heb("💰 שולף נתונים מדף הפירוט...")
            price = None
            daily_change = None
            monthly_change = None
            
            try:
                # 1. חילוץ המחיר
                page.wait_for_selector('#bgLastDeal', timeout=15000)
                price = page.locator('#bgLastDeal').inner_text().strip()
                
                # 2. חילוץ שינוי יומי (לפי ה-ID שמצאת)
                if page.locator('#bgChangePc').count() > 0:
                    daily_change = page.locator('#bgChangePc').inner_text().strip()
                    
                # 3. חילוץ תשואה מתחילת החודש (לפי ה-Class שמצאת)
                if page.locator('.monthlyYield').count() > 0:
                    monthly_change = page.locator('.monthlyYield').inner_text().strip()
                    
            except Exception as wait_err:
                print_heb("❌ שגיאה במציאת הנתונים בדף הפירוט.")
                return None, None, None, asset_name

            # מחזירים עכשיו 4 נתונים במקום 2!
            return price, daily_change, monthly_change, asset_name

        except Exception as e:
            print_heb(f"❌ שגיאה כללית במנוע: {e}")
            return None, None, None, None
        finally:
            browser.close()

if __name__ == "__main__":
    search_query = "nvda" 
    
    # עכשיו הפונקציה מחזירה 4 משתנים
    price, daily, monthly, name = finance_engine_globes(search_query)
    
    if price:
        print("\n" + "="*60)
        print_heb(f"🎯 תוצאה סופית עבור '{search_query}':")
        print_heb(f"[{name}]")
        print_heb(f"💰 מחיר נוכחי: {price}")
        
        # מדפיסים רק אם הנתון קיים בדף
        if daily:
            print_heb(f"📈 שינוי יומי: {daily}")
        if monthly:
            print_heb(f"📅 תשואה מתחילת החודש: {monthly}")
            
        print("="*60 + "\n")