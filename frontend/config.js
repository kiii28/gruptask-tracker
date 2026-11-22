// Alamat backend REST API.
// Default: server yang sama dengan frontend, port 5000 (cocok untuk satu instance EC2).
// Jika backend ada di server/Load Balancer lain, ganti dengan alamat lengkapnya, contoh:
//   window.API_BASE = "http://my-alb-123456.ap-southeast-1.elb.amazonaws.com/api";
window.API_BASE = `http://${location.hostname || "localhost"}:5000/api`;
