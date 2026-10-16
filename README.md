# EIA Gadget Tracking System

Tablet lending and timetable-controlled access to students' personal laptops for
Empower International Academy.

## What it does

- **Tablets** — admins sign tablets out to students and back in; overdue tablets are flagged live.
- **Personal laptops** — students who own a laptop each get a **timetable** (set by an admin, with
  the student's recorded consent). A laptop can only be signed out **during the student's own
  slot** — not before, not after, and not on other days. There is no admin override. Laptops kept
  past the end of the slot appear on the live **Overdue Laptops** page.
- **Student registers** — one for every student in the school (used for name search on the tablet
  sign-out page) and one for students with personal laptops. Admins add or remove students by hand
  or upload a class-list PDF; the names are shown for checking before anything is saved.
- **Activity log** — every action is recorded with the admin who did it.

## Pages

| URL | Access | Description |
|-----|--------|-------------|
| `/` | Public | Home page |
| `/login` | Public | Admin login |
| `/dashboard` | Admin | Tablets + laptops overview, charts, live overdue lists |
| `/tablets` · `/tablets/add` | Admin | Tablet inventory and registration |
| `/tablets/signout` | Admin | Sign a tablet out (type a few letters of the student's name) |
| `/tablets/transactions` · `/tablets/overdue` | Admin | Tablet history and overdue tablets |
| `/students` | Admin | School student register (add / remove / PDF upload) |
| `/laptops` | Admin | **Laptop Desk** — sign laptops out and in |
| `/laptops/overdue` | Admin | Overdue laptops (live) |
| `/laptops/students` | Admin | Laptop students register (add / remove / PDF upload) |
| `/laptops/students/<id>/timetable` | Admin | Edit a student's timetable and record their consent |
| `/activity` | Admin | Team activity log |
| `/api/tablet-status` | Admin API | Live numbers polled by the pages every 10 seconds |

## Laptop timetable rules

1. A student needs at least one slot (day + start time + end time, school time).
2. The student must have agreed to the timetable. Any change to a slot clears the consent, so it
   has to be recorded again before laptops can be signed out.
3. A laptop can be signed out only while a slot is open (start time inclusive, end time exclusive).
4. The laptop is due back at the end of that slot. After that it shows as overdue.
5. Signing a laptop **in** is allowed at any time; a late return is recorded as late.

## Configuration (environment variables)

`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `SECRET_KEY` — required.
`APP_TIMEZONE` — optional (default `Africa/Kampala`).
`ADMIN_USERNAME` / `ADMIN_PASSWORD` — optional emergency login.

Admin accounts are stored (hashed) in the Turso `admins` table.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

## Notes

- Staff attendance has been removed. The old `staff` and `attendance` tables are no longer created
  or used; if they still exist in your Turso database their data is left untouched.
- PDF import reads text-based PDFs (saved from Word or Excel). A scanned picture of a list has no
  text to read — paste the names instead.
