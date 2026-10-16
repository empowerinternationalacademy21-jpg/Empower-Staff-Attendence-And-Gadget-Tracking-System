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
| `/laptops/students/<id>/timetable` | Admin | Edit a student's timetable, attach their timetable PDF, type or read intervals, and record their consent |
| `/activity` | Admin | Team activity log |
| `/api/tablet-status` | Admin API | Live numbers polled by the pages every 10 seconds |

## Laptop timetable rules

1. A student needs at least one slot (day + start time + end time, school time). A day can have
   **several intervals** — e.g. Tuesday 11:30 AM–3:20 PM for lessons *and* Tuesday 7:00–9:00 PM
   for prep — and each interval can have a name ("Prep", "Lessons").
2. The student must have agreed to the timetable. Any change to a slot clears the consent, so it
   has to be recorded again before laptops can be signed out.
3. A laptop can be signed out only while a slot is open (start time inclusive, end time exclusive).
4. The laptop is due back at the end of that slot. After that it shows as overdue.
5. Signing a laptop **in** is allowed at any time; a late return is recorded as late.

## Timetable PDF and quick entry

On a student's timetable page an admin can:

- **Attach the student's timetable PDF** (PDF only, up to 3 MB). It is stored in the database and
  can be viewed, replaced or removed there. Attaching a PDF does not change the timetable by itself.
- **Read the intervals from the PDF**, or **type them** one per line, then check a preview
  (every day, time and name can be edited, rows can be unticked) before anything is saved:

  ```
  Tue 11:30-3:20          -> Tuesday 11:30 AM – 3:20 PM
  Tue prep 7-9            -> Tuesday 7:00 PM – 9:00 PM  (named "Prep")
  Mon-Fri prep 7:9        -> every weekday 7:00 PM – 9:00 PM
  Wed 8am-10am, 2pm-4pm   -> two intervals on Wednesday
  ```

  With no am/pm: a start of 7–11 is morning, 1–6 is afternoon, and an interval named
  prep/evening/night/study is read as evening. A day on its own line applies to the time lines
  under it. Only text PDFs (saved from Word/Excel/Docs) can be read; a scanned picture can still be
  attached, and its intervals typed in.
- Saved intervals either **add to** the current timetable or **replace** it (the admin chooses in
  the preview). As with any timetable change, the student's consent must be recorded again.

Sign-out is allowed only inside an interval and the laptop is due back when that interval ends.
Sign-in is always allowed, so a student can never be stuck holding a laptop.

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
