"""
Tenantify AI — Recommendation Service
Microservice REST API untuk rekomendasi produk UMKM berbasis Content-Based Filtering
menggunakan model multilingual BAAI/bge-m3 + FAISS Index.
"""

import os
import json
import warnings
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from collections import Counter

import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

warnings.filterwarnings("ignore")

# ── Konfigurasi Global ──────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_DATASET_PATH = BASE_DIR / "data" / "products.json"
ORDERS_DATASET_PATH = BASE_DIR / "data" / "orders.json"
DATASET_PATH = PRODUCTS_DATASET_PATH  # Backward compatibility alias
MODEL_NAME = "BAAI/bge-m3"
TOP_N_DEFAULT = 5

COMPLEMENTARY_WEIGHT = 0.40   # Bobot skor komplementer kategori
SEMANTIC_WEIGHT = 0.60        # Bobot cosine similarity embedding

# ── Complementary Category Map ──────────────────────────────────
# Kategori: Minuman, Makanan, Camilan, Gorengan, Dessert
# Score 1.0 = sangat komplementer | 0.0 = tidak relevan
COMPLEMENTARY_MAP: Dict[tuple, float] = {
    # Camilan <-> Minuman
    ("Camilan", "Minuman"): 1.0,
    ("Camilan", "Dessert"): 0.6,
    ("Camilan", "Gorengan"): 0.3,

    # Gorengan <-> Minuman
    ("Gorengan", "Minuman"): 1.0,
    ("Gorengan", "Dessert"): 0.5,
    ("Gorengan", "Camilan"): 0.3,

    # Makanan <-> Minuman & Dessert
    ("Makanan", "Minuman"): 0.9,
    ("Makanan", "Dessert"): 0.8,
    ("Makanan", "Camilan"): 0.4,
    ("Makanan", "Gorengan"): 0.3,

    # Dessert <-> Minuman
    ("Dessert", "Minuman"): 1.0,
    ("Dessert", "Camilan"): 0.5,

    # Minuman <-> Minuman (kompetitif, bukan komplementer)
    ("Minuman", "Minuman"): 0.15,
}


def get_complementary_score(cat_a: str, cat_b: str) -> float:
    """Ambil skor komplementer antara dua kategori (dua arah)."""
    if cat_a == cat_b:
        return COMPLEMENTARY_MAP.get((cat_a, cat_b), 0.2)
    score = COMPLEMENTARY_MAP.get((cat_a, cat_b))
    if score is None:
        score = COMPLEMENTARY_MAP.get((cat_b, cat_a), 0.1)
    return score


# ── Data Models ────────────────────────────────────────────────
@dataclass
class Product:
    id: str
    tenantId: str
    tenantName: str
    boothNumber: str
    name: str
    category: str
    price: int
    description: str
    rating: float
    image: str
    soldCount: int
    isPopular: bool

    @property
    def text_for_embedding(self) -> str:
        return (
            f"{self.name}. "
            f"{self.description} "
            f"Kategori: {self.category}. "
            f"Dari toko: {self.tenantName}."
        )


@dataclass
class OrderItem:
    """Representasi satu item dalam pesanan pelanggan."""
    productName: str
    tenantName: str
    price: int
    quantity: int
    notes: str = ""


@dataclass
class Order:
    """Representasi pesanan pelanggan fullstack."""
    id: str
    pickupCode: str
    orderType: str
    tableNumber: Optional[str]
    tenantNames: List[str]
    items: List[OrderItem]
    subtotal: int
    platformFee: int
    total: int
    status: str
    paymentMethod: str
    createdAt: str


def load_dataset(path: Path) -> List[Product]:
    """Membaca data/products.json dan mengkonversi ke list of Product."""
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset tidak ditemukan di: {path}\n"
            "Pastikan file data/products.json ada di direktori proyek."
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [Product(**item) for item in raw]


def load_orders(path: Path) -> List[Order]:
    """Membaca data/orders.json dan mengkonversi ke list of Order."""
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset pesanan tidak ditemukan di: {path}\n"
            "Pastikan file data/orders.json ada di direktori proyek."
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    orders = []
    for item in raw:
        items = [OrderItem(**it) for it in item["items"]]
        order_dict = {**item, "items": items}
        orders.append(Order(**order_dict))
    return orders


# ── Embedder: Wrapper BAAI/bge-m3 + FAISS ──────────────────────
class Embedder:
    def __init__(self, model_name: str = MODEL_NAME):
        print(f" Memuat model embedding {model_name}...")
        self.model = SentenceTransformer(model_name)
        self.embeddings: Optional[np.ndarray] = None
        self.index: Optional[faiss.IndexFlatIP] = None
        print(f" Model {model_name} berhasil dimuat!")

    def encode(self, texts: List[str], batch_size: int = 32, show_progress: bool = False) -> np.ndarray:
        vecs = self.model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
        )
        return vecs.astype(np.float32)

    def build_index(self, product_list: List[Product]) -> None:
        print(f" Membangun FAISS index untuk {len(product_list)} produk...")
        texts = [p.text_for_embedding for p in product_list]
        self.embeddings = self.encode(texts, show_progress=True)
        dim = self.embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dim)
        self.index.add(self.embeddings)
        print(f" FAISS index siap! ({self.index.ntotal} produk, dimensi {dim})")

    def get_similarities(self, query_idx: int) -> np.ndarray:
        if self.index is None or self.embeddings is None:
            raise RuntimeError("Index belum dibangun.")
        query_vec = self.embeddings[query_idx:query_idx + 1]
        scores, _ = self.index.search(query_vec, self.index.ntotal)
        return scores[0]

    def get_similarities_from_vector(self, query_vec: np.ndarray) -> np.ndarray:
        if self.index is None:
            raise RuntimeError("Index belum dibangun.")
        scores, _ = self.index.search(query_vec.reshape(1, -1), self.index.ntotal)
        return scores[0]


# ── Recommendation Engine ──────────────────────────────────────
@dataclass
class RecommendationResult:
    product: Product
    hybrid_score: float
    semantic_score: float
    complementary_score: float
    reason: str


def _build_reason(query: Product, candidate: Product, comp: float, sem: float) -> str:
    comp_label = (
        "sangat cocok dipadukan" if comp >= 0.9 else
        "cocok dipadukan"        if comp >= 0.7 else
        "cukup cocok dipadukan"  if comp >= 0.5 else
        "bisa dipadukan"
    )
    return (
        f"{candidate.category} ini {comp_label} dengan "
        f"{query.category} '{query.name}' "
        f"(relevansi konteks: {int(sem * 100)}%)."
    )


class Recommender:
    def __init__(self, product_list: List[Product], embedder: Embedder):
        self.products = product_list
        self.embedder = embedder
        self.id_to_idx: Dict[str, int] = {p.id: i for i, p in enumerate(product_list)}
        self.name_to_product: Dict[str, Product] = {p.name: p for p in product_list}

    def recommend(
        self,
        product_id: str,
        top_n: int = TOP_N_DEFAULT,
        exclude_same_tenant: bool = True,
        category_filter: Optional[str] = None,
        exclude_product_ids: Optional[List[str]] = None,
    ) -> List[RecommendationResult]:
        if product_id not in self.id_to_idx:
            raise ValueError(f"Produk '{product_id}' tidak ditemukan di dataset.")

        query_idx = self.id_to_idx[product_id]
        query_product = self.products[query_idx]
        exclude_ids = set(exclude_product_ids or [])
        exclude_ids.add(product_id)

        sim_scores = self.embedder.get_similarities(query_idx)

        results: List[RecommendationResult] = []
        for idx, candidate in enumerate(self.products):
            if candidate.id in exclude_ids:
                continue
            if exclude_same_tenant and candidate.tenantId == query_product.tenantId:
                continue
            if category_filter and candidate.category != category_filter:
                continue

            comp_score = get_complementary_score(query_product.category, candidate.category)
            sem_score = float(np.clip(sim_scores[idx], 0.0, 1.0))
            hybrid = COMPLEMENTARY_WEIGHT * comp_score + SEMANTIC_WEIGHT * sem_score
            reason = _build_reason(query_product, candidate, comp_score, sem_score)

            results.append(RecommendationResult(
                product=candidate,
                hybrid_score=round(hybrid, 4),
                semantic_score=round(sem_score, 4),
                complementary_score=round(comp_score, 4),
                reason=reason,
            ))

        results.sort(key=lambda r: r.hybrid_score, reverse=True)
        return results[:top_n]

    def recommend_from_order(
        self,
        order_items: List[Dict[str, Any]],
        top_n: int = TOP_N_DEFAULT,
    ) -> List[RecommendationResult]:
        ordered_products: List[Product] = []
        for item in order_items:
            product = self.name_to_product.get(item.get("productName", ""))
            if product:
                ordered_products.append(product)

        if not ordered_products:
            raise ValueError("Tidak ada produk dari pesanan yang cocok dengan dataset.")

        ordered_tenant_ids = {p.tenantId for p in ordered_products}
        ordered_product_ids = {p.id for p in ordered_products}

        ordered_idxs = [self.id_to_idx[p.id] for p in ordered_products]
        ordered_vecs = self.embedder.embeddings[ordered_idxs]
        mean_vec = ordered_vecs.mean(axis=0)
        norm = np.linalg.norm(mean_vec)
        if norm > 0:
            mean_vec = (mean_vec / norm).astype(np.float32)

        sim_scores = self.embedder.get_similarities_from_vector(mean_vec)

        category_counts = Counter(p.category for p in ordered_products)
        dominant_category = category_counts.most_common(1)[0][0]

        results: List[RecommendationResult] = []
        for idx, candidate in enumerate(self.products):
            if candidate.id in ordered_product_ids:
                continue
            if candidate.tenantId in ordered_tenant_ids:
                continue

            comp_score = get_complementary_score(dominant_category, candidate.category)
            sem_score = float(np.clip(sim_scores[idx], 0.0, 1.0))
            hybrid = COMPLEMENTARY_WEIGHT * comp_score + SEMANTIC_WEIGHT * sem_score

            reason = (
                f"{candidate.category} ini cocok melengkapi pesananmu "
                f"yang didominasi {dominant_category} "
                f"(relevansi: {int(sem_score * 100)}%)."
            )

            results.append(RecommendationResult(
                product=candidate,
                hybrid_score=round(hybrid, 4),
                semantic_score=round(sem_score, 4),
                complementary_score=round(comp_score, 4),
                reason=reason,
            ))

        results.sort(key=lambda r: r.hybrid_score, reverse=True)
        return results[:top_n]


# ── Pydantic Schemas ───────────────────────────────────────────
class ProductSchema(BaseModel):
    id: str
    tenantId: str
    tenantName: str
    boothNumber: str
    name: str
    category: str
    price: int
    description: str
    rating: float
    image: str
    soldCount: int
    isPopular: bool


class OrderItemSchema(BaseModel):
    productName: str = Field(..., description="Nama produk")
    tenantName: str = Field(..., description="Nama tenant")
    price: int = Field(..., description="Harga satuan")
    quantity: int = Field(1, description="Jumlah")
    notes: str = Field("", description="Catatan pembeli")


class OrderSchema(BaseModel):
    id: str
    pickupCode: str
    orderType: str
    tableNumber: Optional[str] = None
    tenantNames: List[str]
    items: List[OrderItemSchema]
    subtotal: int
    platformFee: int
    total: int
    status: str
    paymentMethod: str
    createdAt: str


class RecommendationItem(BaseModel):
    product: ProductSchema
    hybrid_score: float = Field(description="Skor gabungan [0-1]")
    semantic_score: float = Field(description="Cosine similarity embedding [0-1]")
    complementary_score: float = Field(description="Skor kesesuaian kategori [0-1]")
    reason: str = Field(description="Alasan rekomendasi AI")


class RecommendByProductRequest(BaseModel):
    product_id: str = Field(..., description="ID produk yang dipilih", example="gk-1")
    top_n: int = Field(5, ge=1, le=20, description="Jumlah rekomendasi yang diinginkan")
    exclude_same_tenant: bool = Field(True, description="Kecualikan tenant yang sama untuk pemerataan pedagang")
    category_filter: Optional[str] = Field(None, description="Filter kategori spesifik jika diinginkan")


class RecommendByProductResponse(BaseModel):
    query_product: ProductSchema
    recommendations: List[RecommendationItem]
    total_returned: int


class RecommendByOrderRequest(BaseModel):
    items: List[OrderItemSchema] = Field(..., description="Daftar item pada order object fullstack")
    top_n: int = Field(5, ge=1, le=20, description="Jumlah rekomendasi yang diinginkan")


class RecommendByOrderResponse(BaseModel):
    ordered_products: List[ProductSchema]
    recommendations: List[RecommendationItem]
    total_returned: int


class HealthResponse(BaseModel):
    status: str
    model: str
    total_products: int
    total_tenants: int
    total_orders: int


def product_to_schema(p: Product) -> ProductSchema:
    return ProductSchema(
        id=p.id,
        tenantId=p.tenantId,
        tenantName=p.tenantName,
        boothNumber=p.boothNumber,
        name=p.name,
        category=p.category,
        price=p.price,
        description=p.description,
        rating=p.rating,
        image=p.image,
        soldCount=p.soldCount,
        isPopular=p.isPopular,
    )


def order_to_schema(o: Order) -> OrderSchema:
    return OrderSchema(
        id=o.id,
        pickupCode=o.pickupCode,
        orderType=o.orderType,
        tableNumber=o.tableNumber,
        tenantNames=o.tenantNames,
        items=[
            OrderItemSchema(
                productName=it.productName,
                tenantName=it.tenantName,
                price=it.price,
                quantity=it.quantity,
                notes=it.notes or ""
            ) for it in o.items
        ],
        subtotal=o.subtotal,
        platformFee=o.platformFee,
        total=o.total,
        status=o.status,
        paymentMethod=o.paymentMethod,
        createdAt=o.createdAt,
    )


# ── Inisialisasi Data & Engine ─────────────────────────────────
products = load_dataset(PRODUCTS_DATASET_PATH)
orders = load_orders(ORDERS_DATASET_PATH)
embedder = Embedder(model_name=MODEL_NAME)
embedder.build_index(products)
recommender = Recommender(product_list=products, embedder=embedder)

# ── Inisialisasi FastAPI ───────────────────────────────────────
app = FastAPI(
    title="Tenantify AI — Recommendation API",
    description=(
        "Sistem rekomendasi produk UMKM berbasis Content-Based Filtering & Hybrid Scoring "
        "menggunakan model BAAI/bge-m3. Dirancang untuk pemerataan peluang tenant UMKM."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS Middleware (mengizinkan frontend React/Next.js/Vue di port berapa pun)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ─────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root():
    """Redirect root ke Swagger UI documentation."""
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthResponse, tags=["System"])
def health_check():
    """Cek status server, model, dan total statistik dataset."""
    tenant_ids = {p.tenantId for p in products}
    return HealthResponse(
        status="ok",
        model=MODEL_NAME,
        total_products=len(products),
        total_tenants=len(tenant_ids),
        total_orders=len(orders),
    )


@app.get("/orders", response_model=List[OrderSchema], tags=["Orders"])
def list_orders(
    status: Optional[str] = None,
    order_type: Optional[str] = None,
):
    """
    Ambil semua pesanan pelanggan. Bisa difilter berdasarkan:
    - **status**: Siap Diambil | Sedang Diproses | Selesai
    - **order_type**: Dine-in | Takeaway
    """
    filtered = orders
    if status:
        filtered = [o for o in filtered if o.status.lower() == status.lower()]
    if order_type:
        filtered = [o for o in filtered if order_type.lower() in o.orderType.lower()]
    return [order_to_schema(o) for o in filtered]


@app.get("/orders/{order_id}", response_model=OrderSchema, tags=["Orders"])
def get_order(order_id: str):
    """Ambil detail satu pesanan berdasarkan ID (contoh: `ORD-9201`)."""
    for o in orders:
        if o.id == order_id:
            return order_to_schema(o)
    raise HTTPException(status_code=404, detail=f"Pesanan '{order_id}' tidak ditemukan.")


@app.get("/products", response_model=List[ProductSchema], tags=["Products"])
def list_products(
    category: Optional[str] = None,
    tenant_id: Optional[str] = None,
    is_popular: Optional[bool] = None,
):
    """Ambil list semua produk dengan filter opsional."""
    filtered = products
    if category:
        filtered = [p for p in filtered if p.category.lower() == category.lower()]
    if tenant_id:
        filtered = [p for p in filtered if p.tenantId == tenant_id]
    if is_popular is not None:
        filtered = [p for p in filtered if p.isPopular == is_popular]
    return [product_to_schema(p) for p in filtered]


@app.get("/products/{product_id}", response_model=ProductSchema, tags=["Products"])
def get_product(product_id: str):
    """Ambil detail 1 produk berdasarkan ID."""
    idx = recommender.id_to_idx.get(product_id)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Produk '{product_id}' tidak ditemukan.")
    return product_to_schema(products[idx])


@app.post("/recommend/by-product", response_model=RecommendByProductResponse, tags=["Recommendation"])
def recommend_by_product(req: RecommendByProductRequest):
    """
    Rekomendasi produk komplementer berdasarkan 1 produk yang sedang dilihat user.
    Pemerataan tenant otomatis aktif (exclude produk dari tenant yang sama).
    """
    try:
        results = recommender.recommend(
            product_id=req.product_id,
            top_n=req.top_n,
            exclude_same_tenant=req.exclude_same_tenant,
            category_filter=req.category_filter,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    query_idx = recommender.id_to_idx[req.product_id]
    return RecommendByProductResponse(
        query_product=product_to_schema(products[query_idx]),
        recommendations=[
            RecommendationItem(
                product=product_to_schema(r.product),
                hybrid_score=r.hybrid_score,
                semantic_score=r.semantic_score,
                complementary_score=r.complementary_score,
                reason=r.reason,
            ) for r in results
        ],
        total_returned=len(results),
    )


@app.post("/recommend/by-order", response_model=RecommendByOrderResponse, tags=["Recommendation"])
def recommend_by_order(req: RecommendByOrderRequest):
    """
    Rekomendasi 'Tambahkan ke Pesananmu' berdasarkan item-item yang sedang dipesan.
    Menerima langsung array items dari order object fullstack.
    """
    try:
        results = recommender.recommend_from_order(
            order_items=[item.model_dump() for item in req.items],
            top_n=req.top_n,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    ordered_products = [
        p for item in req.items
        if (p := recommender.name_to_product.get(item.productName))
    ]

    return RecommendByOrderResponse(
        ordered_products=[product_to_schema(p) for p in ordered_products],
        recommendations=[
            RecommendationItem(
                product=product_to_schema(r.product),
                hybrid_score=r.hybrid_score,
                semantic_score=r.semantic_score,
                complementary_score=r.complementary_score,
                reason=r.reason,
            ) for r in results
        ],
        total_returned=len(results),
    )


@app.post("/recommend/by-order-id/{order_id}", response_model=RecommendByOrderResponse, tags=["Recommendation"])
def recommend_by_order_id(order_id: str, top_n: int = Query(5, ge=1, le=20)):
    """
    Rekomendasi langsung berdasarkan ID pesanan yang tersimpan di sistem.
    Cukup kirim order_id (contoh: `ORD-9201`).
    """
    order = next((o for o in orders if o.id == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail=f"Pesanan '{order_id}' tidak ditemukan.")

    order_items = [
        {
            "productName": it.productName,
            "tenantName": it.tenantName,
            "price": it.price,
            "quantity": it.quantity,
            "notes": it.notes or "",
        }
        for it in order.items
    ]

    try:
        results = recommender.recommend_from_order(
            order_items=order_items,
            top_n=top_n,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    ordered_products = [
        p for it in order.items
        if (p := recommender.name_to_product.get(it.productName))
    ]

    return RecommendByOrderResponse(
        ordered_products=[product_to_schema(p) for p in ordered_products],
        recommendations=[
            RecommendationItem(
                product=product_to_schema(r.product),
                hybrid_score=r.hybrid_score,
                semantic_score=r.semantic_score,
                complementary_score=r.complementary_score,
                reason=r.reason,
            ) for r in results
        ],
        total_returned=len(results),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n Menjalankan Tenantify AI Server di port {port}...")
    print(f" Swagger UI: http://localhost:{port}/docs")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
