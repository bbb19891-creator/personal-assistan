# מפתח חיבור ל-Google (OAuth Client), פעם אחת, כ-10 דקות

1. נכנסים ל-https://console.cloud.google.com/projectcreate עם החשבון שלך ויוצרים פרויקט חדש.
2. מפעילים (Enable) שלושה שירותים: Gmail API, Google Calendar API ו-Google Drive API.
   - https://console.cloud.google.com/apis/library/gmail.googleapis.com
   - https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
   - https://console.cloud.google.com/apis/library/drive.googleapis.com
3. נכנסים ל-https://console.cloud.google.com/auth/branding ולוחצים Get started. ממלאים שם אפליקציה והמייל שלך, בוחרים Audience = **External**, ומאשרים.
4. בדף **Audience** מוסיפים את המייל שלך ל-Test users ולוחצים **Publish app** (In production). בלי השלב הזה החיבור יתנתק כל 7 ימים.
5. נכנסים ל-https://console.cloud.google.com/auth/clients, לוחצים Create client ובוחרים **Desktop app**.
6. מעתיקים את Client ID ואת Client secret ומדביקים אותם בדף ההגדרות, בשלב Google.
7. לוחצים "חבר את Google", בוחרים את החשבון ומאשרים את כל ההרשאות. בסוף הדפדפן יגיע לכתובת `http://localhost:8765/...` שלא נטענת, וזה צפוי. מעתיקים את הכתובת המלאה ומדביקים אותה בדף.

ההרשאות שהבוט מבקש: יומן (קריאה וכתיבה), Gmail (קריאה ושליחה), ו-Drive (ארכיון מסמכים). הטוקן נשמר רק בשרת שלך, בקובץ `/opt/agent/data/google_accounts.json`.
