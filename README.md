# EIA Gadget Tracking System

Tablet lending and timetable-controlled access to students' personal gadgets for
Empower International Academy.

## What it does

- **Tablets** — admins sign tablets out to students and back in; overdue tablets are flagged live.
- **Personal gadgets** — students who bring their own gadget (a **Laptop** or a **Tablet**, chosen by the
  admin when registering them) each get a **timetable** (set by an admin, with
  the student's recorded consent). A gadget can only be signed out **during the student's own
  slot** — not before, not after, and not on other days. There is no admin override. Gadgets kept
  past the end of the slot appear on the live **Overdue Gadgets** page.
- **Student registers** — one for every student in the school (used for name search on the tablet
  sign-out page) and one for students with personal gadgets. Admins add or remove students by hand
  or upload a class-list PDF; the names are shown for checking before anything is saved.
- **Promoting classes** — at the start of a year, **Promote Classes** moves every student to the next
  class with one button (after a review screen), or one student / the ticked students at a time. The
  last promotion can be undone. See below.
- **Activity log** — every action is recorded with the admin who did it.

## Pages

| URL | Access | Description |
|-----|--------|-------------|
| `/` | Public | Home page |
| `/login` | Public | Admin login |
| `/dashboard` | Admin | Tablets + gadgets overview, charts, live overdue lists |
| `/tablets` · `/tablets/add` | Admin | Tablet inventory and registration |
| `/tablets/signout` | Admin | Sign a tablet out (type a few letters of the student's name) |
| `/tablets/transactions` · `/tablets/overdue` | Admin | Tablet history and overdue tablets |
| `/students` · `/students/promote` | Admin | School student register (add / remove / PDF upload) and class promotion |
| `/gadgets` | Admin | **Gadget Desk** — sign gadgets out and in |
| `/gadgets/overdue` | Admin | Overdue gadgets (live) |
| `/gadgets/students` · `/gadgets/students/promote` | Admin | Gadget students register (choose Laptop or Tablet; add / remove / PDF upload) and class promotion |
| `/gadgets/students/<id>/timetable` | Admin | Edit a student's timetable, attach their timetable PDF, type or read intervals, and record their consent |
| `/blacklist` | Admin | Students blocked for returning a tablet or gadget more than 20 minutes late |
| `/activity` | Admin | Team activity log |
| `/api/tablet-status` | Admin API | Live numbers polled by the pages every 10 seconds |

## Fair-use rules (enforced by the server)

1. **One tablet per student per school day.** If an admin tries to sign a tablet out to a student who
   has already had one today (even if it was returned), the sign-out is denied with a clear notice,
   nothing is issued, and the attempt is written to the activity log. The student can borrow again
   the next school day (the day follows the school's time zone, so it resets at local midnight).
2. **Blacklist.** A tablet or personal gadget returned **more than 20 minutes after it was due**
   puts the student on the **Blacklist** page automatically. For **4 days** from the moment it was
   signed back in, the student cannot sign out a tablet *or* a gadget. When the 4 days have passed
   the block lifts by itself and the entry moves to *Past entries*.
3. A student who is still holding a tablet or gadget that is more than 20 minutes overdue cannot
   take another one until it is returned.

Students are matched by **name + class**, ignoring capital letters, extra spaces and punctuation
(`Mukasa  Grace / P.5` is the same person as `mukasa grace / P5`). Two different students with the
same name in different classes are different people. Only returns made after this feature was
added are counted. Search lists on the sign-out pages tag blacklisted students so admins see it
before pressing the button. The two numbers can be changed with the optional environment variables
`BLACKLIST_LATE_MINUTES` (default 20) and `BLACKLIST_DAYS` (default 4).

## Promoting students to the next class

Both registers have a **Promote Classes** button. The page lists every class with the class it
will move to, for example `P.5 → P.6`, `P.7 → S.1`, `Baby Class → Middle Class → Top Class → P.1`.
Names such as `Primary Five`, `Senior 3 B` or `Year 7` are understood too, and a stream letter is
kept (`S.2A → S.3A`). The admin can change any class's destination, choose **Leave the school**
for a final class, or **Keep in this class**, and then presses **Promote the whole school**.

- If the school has no secondary classes, `P.7` is offered as *Leave the school* instead of `S.1`.
- A class name the system does not recognise is never guessed — it is set to *Keep* until the admin
  chooses.
- Students who leave are hidden from the register, not deleted.
- Each student can also be promoted with the **Promote** button on their row, several can be
  ticked and promoted together, or moved to any class typed in (**Move selected to class**).
- If two students have the same name, one that would end up beside a namesake in the same class is
  skipped and reported rather than forced. A student whose gadget is still signed out cannot be
  made to leave the school.
- Every change is recorded, and the most recent one can be reversed with **Undo** (students whose
  class was edited afterwards are left alone).

## Gadget timetable rules

1. A student needs at least one slot (day + start time + end time, school time). A day can have
   **several intervals** — e.g. Tuesday 11:30 AM–3:20 PM for lessons *and* Tuesday 7:00–9:00 PM
   for prep — and each interval can have a name ("Prep", "Lessons").
2. The student must have agreed to the timetable. Any change to a slot clears the consent, so it
   has to be recorded again before gadgets can be signed out.
3. A gadget can be signed out only while a slot is open (start time inclusive, end time exclusive).
4. The gadget is due back at the end of that slot. After that it shows as overdue.
5. Signing a gadget **in** is allowed at any time; a late return is recorded as late.

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

Sign-out is allowed only inside an interval and the gadget is due back when that interval ends.
Sign-in is always allowed, so a student can never be stuck holding a gadget.

## Tablet sign-out

Admins pick a tablet, type a few letters of the student's name (the full name and class fill in from
the school register), choose the return time and sign it out. There are no accessory options.
(Older records that still carry charger / earphones / USB data keep it in the database; it is no
longer shown or collected.)

## Older web addresses

The gadget pages used to live under `/laptops`. Those addresses still work and send you to the
matching `/gadgets` page.

## Configuration (environment variables)

`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `SECRET_KEY` — required.
`APP_TIMEZONE` — optional (default `Africa/Kampala`).
`ADMIN_USERNAME` / `ADMIN_PASSWORD` — optional emergency login.
`BLACKLIST_LATE_MINUTES` / `BLACKLIST_DAYS` — optional (defaults 20 and 4), see Fair-use rules.

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
