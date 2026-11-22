# GroupTask Tracker

Task board kelompok dengan login, kolaborasi, dan status online, untuk Tugas 9 Komputasi Awan dan Terdistribusi.

- **Backend**: Flask REST API (port 5000). Data (user, proyek, anggota, tugas) disimpan **di memori sebagai dict**, tanpa database, sesuai soal poin 2.b. Data kosong lagi setiap server restart.
- **Frontend**: HTML/CSS/JS statis (tanpa build).

```
grouptask-tracker/
├── backend/   app.py, requirements.txt
├── frontend/  index.html, config.js
└── deploy/    setup.sh, user-data.sh, nginx.conf, grouptask.service   (hanya untuk AWS)
```

## Jalankan di komputer sendiri

```bash
# terminal 1 - backend
cd backend && pip install -r requirements.txt
python app.py                  # tambahkan SEED_DEMO=1 untuk akun demo: demo@example.com / Demo12345

# terminal 2 - frontend
cd frontend && python -m http.server 8080     # buka http://localhost:8080
```

Tanpa konfigurasi email, aplikasi berjalan dalam **mode dev**: kode OTP ditampilkan di halaman verifikasi (dan di terminal backend), serta undangan kolaborator muncul sebagai banner di akun penerima setelah ia login. Dengan begitu semua fitur bisa dicoba tanpa SMTP.

## Fitur

| Fitur | Keterangan |
| --- | --- |
| Register + OTP | Username, email, password, lalu kode 6 digit dikirim ke email (berlaku 10 menit, maks 5 percobaan). Setelah konfirmasi kembali ke login. |
| Login | Email + password, atau Google. Token sesi 7 hari. |
| Profil | Klik lingkaran foto di kanan atas: ubah foto, username, NIM, password, dan Log out. |
| Cari dan undang kolaborator | Kelola Tim: ketik username (hasil muncul otomatis, maks 5, awalan huruf diprioritaskan) atau email lengkap. Jika tidak ditemukan tampil pesan; email yang belum terdaftar masih bisa diundang lewat email. Sampai diterima, anggota tampil sebagai **Pending**. Setelah penerima klik link di email (atau tombol Terima di banner), nama berubah menjadi username. |
| Penanggung jawab ganda | Satu tugas bisa dipegang beberapa anggota aktif (relasi Task-Member many-to-many). Kartu menampilkan avatar bertumpuk. |
| Info anggota | Klik anggota: username, NIM, role, deskripsi. Role dan deskripsi bisa diubah semua anggota proyek dan hanya berlaku di proyek itu (default "-"). |
| Status online | Foto anggota di atas tombol Edit/Hapus proyek. Online = terang + titik hijau, offline = redup. Dianggap online jika ada aktivitas dalam 45 detik terakhir (halaman memperbarui diri tiap 15 detik). |
| Progress Kalkulator | Progres proyek = Done / total tugas, dihitung ulang otomatis setiap tugas berubah. |

Hak akses: proyek hanya terlihat oleh anggotanya. Hanya pemilik yang boleh menghapus proyek dan mengeluarkan anggota. Anggota non-pemilik boleh keluar sendiri.

## Konfigurasi (environment variable)

| Variabel | Fungsi |
| --- | --- |
| `SECRET_KEY` | Kunci penanda token. **Harus sama di semua instance.** |
| `APP_URL` | Alamat frontend untuk link di email undangan, mis. `http://<ip-ec2>` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Pengiriman email OTP dan undangan. Kosong = mode dev. |
| `GOOGLE_CLIENT_ID` | Mengaktifkan login Google. Kosong = tombol Google menampilkan pesan "belum dikonfigurasi". |
| `SEED_DEMO=1` | Membuat akun dan proyek contoh. |

**Email lewat Gmail**: aktifkan verifikasi 2 langkah, buat *App password*, lalu `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` dan `SMTP_FROM` = alamat Gmail, `SMTP_PASSWORD` = app password. Alternatif di AWS adalah Amazon SES, tetapi akun baru berstatus sandbox sehingga hanya bisa mengirim ke email yang sudah diverifikasi.

**Login Google**: di Google Cloud Console buat *OAuth client ID* tipe Web, isi *Authorized JavaScript origins* dengan alamat frontend, lalu set `GOOGLE_CLIENT_ID`. `http://localhost:8080` diizinkan untuk uji lokal, tetapi untuk server Google mewajibkan **domain ber-HTTPS** (alamat IP EC2 dengan http tidak diterima).

## Deploy ke AWS EC2

```bash
cd grouptask-tracker
sudo bash deploy/setup.sh        # nginx + gunicorn + systemd, aplikasi di port 80
sudo nano /etc/grouptask.env     # isi APP_URL, SMTP_*, GOOGLE_CLIENT_ID
sudo systemctl restart grouptask
```

Security Group: buka port 80 dan 22 (443 jika memakai HTTPS). `deploy/user-data.sh` dipakai untuk instance yang memasang diri sendiri saat launch (isi `REPO_URL`, `SECRET_KEY`, `APP_URL`), atau buat AMI dari instance yang sudah jalan.

## Load Balancer dan keterbatasannya

- Health check: `GET /api/health`. Di pojok bawah halaman ada "Dilayani oleh server: ..." yang menunjukkan instance yang menjawab.
- Karena tanpa database, **setiap instance punya data sendiri**, termasuk akun dan sesi login. Di belakang Application Load Balancer, aktifkan **stickiness** (Target Group > Attributes > Stickiness) agar satu pengguna selalu dilayani instance yang sama. Tanpa itu, pengguna akan terasa "keluar sendiri" saat request jatuh ke instance lain.
- `SECRET_KEY` yang sama di semua instance diperlukan agar token valid, tetapi akun tetap hanya ada di instance tempat register dilakukan.
- Gunakan satu worker gunicorn (`-w 1`), karena data ada di memori proses.

## Endpoint

Semua endpoint selain `/api/health`, `/api/config`, dan `/api/auth/*` memerlukan header `Authorization: Bearer <token>`.

| Method | Path | Fungsi |
| --- | --- | --- |
| POST | `/api/auth/register`, `/api/auth/resend-otp`, `/api/auth/verify-otp` | Daftar dan verifikasi OTP |
| POST | `/api/auth/login`, `/api/auth/google`, `/api/auth/logout` | Sesi |
| GET / PUT | `/api/me` | Profil (`username`, `nim`, `photo`) |
| PUT | `/api/me/password` | Ubah password |
| GET / POST | `/api/projects` | Proyek milik saya / buat proyek |
| GET / PUT / DELETE | `/api/projects/<id>` | Detail / ubah / hapus (pemilik) |
| GET | `/api/projects/<id>/board` | Proyek + anggota (status online) + tugas |
| GET / POST | `/api/projects/<id>/members` | Daftar anggota / undang lewat `{"user_id": ...}`, `{"username": ...}`, atau `{"email": ...}` |
| GET | `/api/projects/<id>/users/search?q=` | Cari pengguna terdaftar (maks 5). Username: cocok sebagian; email: harus persis. Hasil tanpa email. |
| GET / PUT / DELETE | `/api/members/<id>` | Detail / ubah `role`, `description` / keluarkan atau batalkan undangan |
| GET | `/api/invitations` | Undangan untuk saya |
| POST | `/api/invitations/accept`, `/api/invitations/decline` | `{"token": ...}` atau `{"member_id": ...}` |
| GET / POST | `/api/tasks` | Daftar (filter `project_id`, `member_id`, `status`) / buat (`member_ids` berupa daftar) |
| GET / PUT / DELETE | `/api/tasks/<id>` | Detail / ubah / hapus |
| PATCH | `/api/tasks/<id>/assign` | Atur penanggung jawab: `{"member_ids": ["M01", "M02"]}` |
| PATCH | `/api/tasks/<id>/status` | Ubah status, progres proyek dihitung ulang |
