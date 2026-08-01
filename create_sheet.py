import os, sqlite3
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

cred = '/app/google_credentials.json'
scopes = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
creds = Credentials.from_service_account_file(cred, scopes=scopes)
sheets = build('sheets', 'v4', credentials=creds)
drive = build('drive', 'v3', credentials=creds)

# Create spreadsheet via Sheets API (works even with 0 Drive quota)
body = {
    'properties': {'title': 'Clients AshuraCosm'},
    'sheets': [{'properties': {'title': 'Clients', 'gridProperties': {'frozenRowCount': 1}}}]
}

try:
    result = sheets.spreadsheets().create(body=body).execute()
    sid = result['spreadsheetId']
    print('Created: ' + sid)
    print('URL: https://docs.google.com/spreadsheets/d/' + sid)

    # Headers
    headers = [['TG ID', 'Phone', 'Name', 'Visits', 'Last Procedure', 'Last Date', 'Last Amount', 'Total Spent', 'Next Visit', 'Status', 'Bot Wrote', 'Notes']]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range='A1:L1', valueInputOption='RAW', body={'values': headers}
    ).execute()
    print('Headers set')

    # Format header
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={'requests': [{'repeatCell': {
            'range': {'sheetId': 0, 'startRowIndex': 0, 'endRowIndex': 1},
            'cell': {'userEnteredFormat': {
                'backgroundColor': {'red': 0.15, 'green': 0.5, 'blue': 0.7},
                'textFormat': {'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}, 'bold': True}
            }},
            'fields': 'userEnteredFormat(backgroundColor,textFormat)'
        }}]}
    ).execute()
    print('Formatted')

    # Populate with clients
    conn = sqlite3.connect('data/bot.db')
    c = conn.cursor()
    c.execute(open('/tmp/populate.sql').read())
    rows = []
    for row in c.fetchall():
        tg_id, phone, name, bonus, visits, last_proc, last_date, last_amt, total, next_v, bot_w = row
        rows.append([
            str(tg_id), phone or '', name or '', str(visits or 0),
            last_proc or '', str(last_date or '')[:10], str(last_amt or 0),
            str(total or 0), str(next_v or '')[:10],
            'New' if not visits else 'Client', str(bot_w or '')[:10], ''
        ])
    if rows:
        sheets.spreadsheets().values().update(
            spreadsheetId=sid, range='A2', valueInputOption='RAW', body={'values': rows}
        ).execute()
        print('Populated: ' + str(len(rows)) + ' clients')
    conn.close()

    # Share with admin
    drive.permissions().create(
        fileId=sid,
        body={'type': 'user', 'role': 'writer', 'emailAddress': 'ash001005@gmail.com'},
        sendNotificationEmail=False
    ).execute()
    print('Shared with ash001005@gmail.com')

    print('SPREADSHEET_ID=' + sid)

except Exception as e:
    print('Error: ' + str(e))
    import traceback
    traceback.print_exc()
