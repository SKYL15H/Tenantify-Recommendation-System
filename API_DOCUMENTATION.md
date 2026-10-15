# 🛒 Tenantify AI — API Documentation (Untuk Fullstack)

Dokumentasi REST API Rekomendasi Tenantify berbasis AI Hybrid Scoring (`BAAI/bge-m3` + FAISS + Category Complementary).

---

## 🚀 Cara Menjalankan AI Service

Pastikan library sudah terinstall, lalu jalankan di terminal:

```bash
# 1. Install dependencies (sekali saja)
pip install -r requirements.txt

# 2. Jalankan server
python main.py
```

* **Base URL:** `http://localhost:8000`
* **Interactive Swagger UI:** [`http://localhost:8000/docs`](http://localhost:8000/docs) (bisa coba langsung via browser)
* **CORS:** Sudah di-enable untuk semua origin (`*`), jadi aman dipanggil dari React / Next.js / Vue (port 3000, 5173, dll).

---

## 📌 Endpoints

### 1. Health Check
Mengecek status server dan model AI.

* **Method:** `GET`
* **URL:** `/health`
* **Response `200 OK`:**
```json
{
  "status": "ok",
  "model": "BAAI/bge-m3",
  "total_products": 48,
  "total_tenants": 12
}
```

---

### 2. Rekomendasi Berdasarkan Satu Produk
> **Gunakan pada:** Halaman detail produk / modal produk (seksi *"Cocok dinikmati bersama"*).

* **Method:** `POST`
* **URL:** `/recommend/by-product`
* **Request Headers:** `Content-Type: application/json`
* **Request Body:**
```json
{
  "product_id": "gk-1",
  "top_n": 5,
  "exclude_same_tenant": true,
  "category_filter": null
}
```

| Field | Type | Default | Keterangan |
|---|---|---|---|
| `product_id` | `string` | *(wajib)* | ID produk yang sedang dilihat user (contoh: `"gk-1"`, `"cp-1"`) |
| `top_n` | `number` | `5` | Jumlah rekomendasi yang dikembalikan (1 - 20) |
| `exclude_same_tenant` | `boolean` | `true` | `true` = hanya rekomendasi dari tenant lain (pemerataan UMKM) |
| `category_filter` | `string` / `null` | `null` | Filter kategori spesifik: `"Minuman"`, `"Makanan"`, `"Camilan"`, `"Dessert"`, dll |

* **Response `200 OK`:**
```json
{
  "query_product": {
    "id": "gk-1",
    "name": "Kue Ikan Isi Coklat",
    "tenantId": "tenant-7",
    "tenantName": "Gorengan & Kue Ikan Kak Sari",
    "boothNumber": "Blok B-07",
    "category": "Camilan",
    "price": 5000,
    "description": "Kue ikan crispy berbentuk ikan dengan isian coklat lumer...",
    "rating": 4.8,
    "image": "/images/kue-ikan-coklat.jpg",
    "soldCount": 450,
    "isPopular": true
  },
  "recommendations": [
    {
      "product": {
        "id": "cp-1",
        "name": "Kopi Susu Gula Aren",
        "tenantId": "tenant-1",
        "tenantName": "Kopi & Roti Nusantara",
        "boothNumber": "Blok A-04",
        "category": "Minuman",
        "price": 18000,
        "description": "Kopi espresso pilihan dipadukan susu creamy dan sirup gula aren asli...",
        "rating": 4.9,
        "image": "/images/kopi-susu.jpg",
        "soldCount": 1240,
        "isPopular": true
      },
      "hybrid_score": 1.0,
      "semantic_score": 1.0,
      "complementary_score": 1.0,
      "reason": "Minuman ini sangat cocok dipadukan dengan Camilan 'Kue Ikan Isi Coklat' (relevansi konteks: 100%)."
    }
  ],
  "total_returned": 1
}
```

---

### 3. Rekomendasi Berdasarkan Pesanan Aktif (Cart / Checkout)
> **Gunakan pada:** Halaman keranjang belanja / ringkasan order (seksi *"Tambahkan ke pesananmu"*).

* **Method:** `POST`
* **URL:** `/recommend/by-order`
* **Request Body:** (Bisa langsung passing array `items` dari order object frontend)
```json
{
  "items": [
    {
      "productName": "Kopi Susu Gula Aren",
      "tenantName": "Kopi & Roti Nusantara",
      "price": 18000,
      "quantity": 1,
      "notes": "Gula aren sedikit"
    },
    {
      "productName": "Paket Ayam Geprek Krispi + Nasi",
      "tenantName": "Ayam Geprek Sambal Korek",
      "price": 25000,
      "quantity": 1,
      "notes": "Pedas sedang"
    }
  ],
  "top_n": 5
}
```

* **Response `200 OK`:**
```json
{
  "ordered_products": [ ... ],
  "recommendations": [
    {
      "product": {
        "id": "jp-1",
        "name": "Klepon Pandan Original",
        "tenantName": "Jajanan Pasar Mbok Darmi",
        "boothNumber": "Blok A-09",
        "category": "Camilan",
        "price": 8000,
        "rating": 4.9,
        ...
      },
      "hybrid_score": 0.814,
      "semantic_score": 0.69,
      "complementary_score": 1.0,
      "reason": "Camilan ini cocok melengkapi pesananmu yang didominasi Minuman (relevansi: 69%)."
    }
  ],
  "total_returned": 1
}
```

---

## 💻 Contoh Snippet Kode Frontend (Next.js / React)

### Menggunakan Native `fetch` (JavaScript / TypeScript)
```typescript
const AI_BASE_URL = "http://localhost:8000";

// 1. Rekomendasi by Product
export async function getRecommendationsByProduct(productId: string, topN = 5) {
  const res = await fetch(`${AI_BASE_URL}/recommend/by-product`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      product_id: productId,
      top_n: topN,
      exclude_same_tenant: true,
    }),
  });
  if (!res.ok) throw new Error("Gagal mengambil rekomendasi AI");
  return res.json();
}

// 2. Rekomendasi by Active Order
export async function getRecommendationsByOrder(orderItems: any[], topN = 5) {
  const res = await fetch(`${AI_BASE_URL}/recommend/by-order`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      items: orderItems,
      top_n: topN,
    }),
  });
  if (!res.ok) throw new Error("Gagal mengambil rekomendasi cart");
  return res.json();
}
```
