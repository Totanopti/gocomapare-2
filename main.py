from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
import keepa
import requests
import os
from dotenv import load_dotenv  # Optional: for local development

# Load environment variables (for local development)
load_dotenv()

app = FastAPI(
    title="Amazon Storefront Analyzer API",
    description="Analyze seller storefronts by Seller ID, optionally filtered by Category ID, using Keepa + OptiSage with strict category filtering.",
    version="1.1.1"
)

# --- Environment Variable Configuration ---
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY")
OPTISAGE_TOKEN = os.getenv("OPTISAGE_TOKEN")

# Validate that required environment variables are set
if not KEEPA_API_KEY:
    raise RuntimeError("KEEPA_API_KEY environment variable is required")
if not OPTISAGE_TOKEN:
    raise RuntimeError("OPTISAGE_TOKEN environment variable is required")

MAX_PRODUCTS = 30

# Marketplace domain mapping
DOMAIN_MAP = {
    "US": "US",
    "UK": "UK",
    "DE": "DE",
    "FR": "FR",
    "JP": "JP",
    "CA": "CA"
}

MARKETPLACE_NUMERIC = {
    'US': 1,
    'UK': 3,
    'DE': 4,
    'FR': 5,
    'JP': 6,
    'CA': 7
}

# --- Request Model ---
class SellerRequest(BaseModel):
    seller_id: str = Field(..., description="The Amazon Seller ID (e.g., A3I41TQZK5ELJT).")
    marketplace: str = Field("US", description="The Amazon marketplace domain (e.g., US, UK, DE).")
    category_id: Optional[int] = Field(None, description="Optional: A specific Keepa Category ID to restrict the search (e.g., 3760911).")

# --- OptiSage helper ---
class OptiSageAPI:
    def __init__(self, bearer_token: str):
        self.bearer_token = bearer_token
        self.base_url = "https://api-staging.optisage.ai"

    def check_seller_eligibility(self, seller_id: str, asins: List[str], marketplace: str) -> Dict:
        if not self.bearer_token:
            return {'success': False, 'error': 'No OptiSage token provided'}

        url = f"{self.base_url}/api/go-compare/seller-eligibility"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "amazon_seller_id": seller_id,
            "marketplace_id": MARKETPLACE_NUMERIC.get(marketplace, 1),
            "asins": asins
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                return {'success': True, 'data': resp.json()}
            else:
                return {'success': False, 'error': f"API Error {resp.status_code}", 'details': resp.text}
        except requests.RequestException as e:
            return {'success': False, 'error': f"Request failed: {str(e)}"}

# --- Keepa helpers ---
def get_seller_asins(keepa_key: str, seller_id: str, domain: str, max_asins: int = 50, category_id: Optional[int] = None) -> List[str]:
    try:
        api = keepa.Keepa(keepa_key)
        product_parms = {'sellerIds': seller_id, 'pageSize': max_asins}
        
        if category_id is not None:
            product_parms['category'] = str(category_id)
        
        asins = api.product_finder(product_parms, domain=domain)
        return asins[:max_asins] if asins else []
    except Exception as e:
        raise RuntimeError(f"ASIN fetch error: {e}")

def get_product_details_batch(keepa_key: str, asins: List[str], domain: str) -> List[Dict]:
    if not asins:
        return []
    try:
        api = keepa.Keepa(keepa_key)
        products = api.query(asins, domain=domain, stats=90)
        product_details = []
        for product in products:
            if 'asin' not in product:
                continue
            
            stats = product.get('stats', {})
            current_data = stats.get('current', [0]*25)
            
            # --- IMAGE URL EXTRACTION ---
            image_url = None
            if product.get('image'):
                image_url = product['image']
            elif product.get('imagesCSV'):
                image_path_segment = product['imagesCSV'].split(',')[0]
                image_url = f"https://m.media-amazon.com/images/I/{image_path_segment}"

            # --- ROBUST PRICE EXTRACTION LOGIC ---
            current_price_cents = 0
            if current_data[0] > 0: current_price_cents = current_data[0]
            elif current_data[13] > 0: current_price_cents = current_data[13]
            elif current_data[7] > 0: current_price_cents = current_data[7]
            elif current_data[1] > 0: current_price_cents = current_data[1]

            current_price = current_price_cents / 100 if current_price_cents > 0 else None
            sales_rank = current_data[3] if isinstance(current_data, list) and len(current_data) > 3 and current_data[3] > 0 else None
            rating_value = product.get('rating', 0) / 10.0 if product.get('rating') is not None else 0.0
            review_count = product.get('reviewCount', 0)
            
            details = {
                'asin': product.get('asin'),
                'title': product.get('title', 'N/A'),
                'brand': product.get('brand', 'N/A'),
                'category_id': product.get('rootCategory', 'N/A'),
                'category_name': None,
                'sales_rank': sales_rank or 0,
                'rating_value': rating_value,  
                'review_count': review_count,
                'rating_display': f"{rating_value:.1f}/5 ({review_count:,} reviews)", 
                'current_price': f"${current_price:.2f}" if current_price else 'N/A',
                'image_url': image_url
            }
            product_details.append(details)
        return product_details
    except Exception as e:
        raise RuntimeError(f"Product details error: {e}")

def get_category_name(keepa_key: str, category_id: int, domain: str) -> str:
    try:
        api = keepa.Keepa(keepa_key)
        categories = api.category_lookup(category_id, domain=domain)
        category_obj = categories.get(str(category_id))
        return category_obj.get('name', 'Unknown Category') if category_obj else 'Unknown Category'
    except:
        return 'Category Lookup Failed'

def parse_eligibility_result(eligibility_data: Dict, asin: str) -> Dict:
    if not eligibility_data:
        return {'status': '❓ API Error', 'reason': 'No eligibility data received'}
    try:
        if 'data' in eligibility_data and isinstance(eligibility_data['data'], list):
            for item in eligibility_data['data']:
                if item.get('asin') == asin:
                    is_eligible = item.get('isEligible', False)
                    if is_eligible:
                        return {'status': '✅ Eligible', 'reason': 'Seller is eligible to sell this product'}
                    else:
                        return {'status': '❌ Restricted', 'reason': 'Seller is not eligible to sell this product'}
        elif not eligibility_data.get('success', True):
            error_msg = eligibility_data.get('error', 'OptiSage API failed')
            details = eligibility_data.get('details', '')
            return {'status': '❓ API Error', 'reason': f'{error_msg}: {details[:50]}...'}
            
        return {'status': '⚠️ Not Found', 'reason': 'ASIN not found in eligibility results'}
    except Exception as e:
        return {'status': '🔧 Parse Error', 'reason': f'Failed to parse eligibility: {str(e)}'}

# --- Main endpoint with manual filtering ---
@app.post("/analyze_seller", summary="Analyze seller storefront")
def analyze_seller(req: SellerRequest):
    marketplace = req.marketplace.upper()
    if marketplace not in DOMAIN_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported marketplace '{req.marketplace}'. Use one of: {list(DOMAIN_MAP.keys())}")

    requested_category_id_str = str(req.category_id) if req.category_id else None

    # 1) Get ASINs (Keepa filtering applied here, but might be loose)
    try:
        asins = get_seller_asins(
            KEEPA_API_KEY,  # Using environment variable
            req.seller_id, 
            domain=marketplace, 
            max_asins=MAX_PRODUCTS,
            category_id=req.category_id
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Keepa ASIN Fetch Error: {str(e)}")

    if not asins:
        filter_detail = f" in Category ID {req.category_id}" if req.category_id else ""
        raise HTTPException(status_code=404, detail=f"No ASINs found for this seller{filter_detail}.")

    # 2) Get full product details
    try:
        products = get_product_details_batch(KEEPA_API_KEY, asins, domain=marketplace)  # Using environment variable
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Keepa Product Details Error: {str(e)}")

    # 3) Add category names AND **STRICTLY FILTER**
    final_products = []
    
    for p in products:
        cid = p.get('category_id')
        
        # 3a. Get Category Name
        if cid and cid != 'N/A':
            try:
                category_name = get_category_name(KEEPA_API_KEY, int(cid), domain=marketplace)  # Using environment variable
                p['category_name'] = category_name
            except Exception:
                p['category_name'] = 'Category lookup failed'
        else:
            p['category_name'] = 'Unknown'
            
        # 3b. 🟢 ENFORCE MANUAL FILTERING 🟢
        # Only append the product if:
        # 1. No category filter was requested (i.e., requested_category_id_str is None)
        # OR
        # 2. The fetched product's category ID matches the requested category ID
        if requested_category_id_str is None or str(cid) == requested_category_id_str:
            final_products.append(p)
    
    # Check if any products remain after strict filtering
    if not final_products and requested_category_id_str:
        raise HTTPException(status_code=404, detail=f"No products matched the Seller ID and the strict filter for Category ID {req.category_id} after fetching.")

    # 4) Check eligibility on the final, filtered list
    filtered_asins = [p.get('asin') for p in final_products]
    
    opti = OptiSageAPI(OPTISAGE_TOKEN)  # Using environment variable
    eligibility_data = opti.check_seller_eligibility(req.seller_id, filtered_asins, marketplace)
    
    if not eligibility_data.get('success'):
        # If OptiSage fails, use the error data for parsing (as implemented in the helper)
        eligibility_data = eligibility_data

    # 5) Format response
    formatted = []
    for idx, p in enumerate(final_products): # Iterate over final_products
        asin = p.get('asin')
        parsed = parse_eligibility_result(eligibility_data, asin)
        formatted.append({
            "index": idx + 1,
            "ASIN": asin,
            "Title": p.get('title', 'N/A'),
            "Brand": p.get('brand', 'N/A'),
            "Category": p.get('category_name', 'Unknown'),
            "SalesRank": p.get('sales_rank'),
            "Velocity": "🚀 YES (< 50K)" if p.get('sales_rank', 999999) < 50000 else "SLOW (> 50K)",
            "Eligibility": parsed['status'],
            "Comment": parsed['reason'],
            "Rating": p.get('rating_display', '0.0/5 (0 reviews)'), 
            "Reviews": str(p.get('review_count', 'N/A')), 
            "Price": p.get('current_price', 'N/A'),
            "ImageURL": p.get('image_url', 'N/A')
        })

    return {
        "Seller": req.seller_id,
        "Marketplace": marketplace,
        "Filter_Category_ID": req.category_id if req.category_id else 'None',
        "Total_Products": len(formatted),
        "Products": formatted
    }
