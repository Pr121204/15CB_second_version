import cv2, numpy as np
from PIL import Image
import io

def enhance_image_for_ocr(pil_img):
    # basic thresholding and denoise using OpenCV
    arr = np.array(pil_img.convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    # resize if small
    h, w = gray.shape
    if w < 1200:
        scale = 1200 / float(w)
        gray = cv2.resize(gray, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_LINEAR)
    # denoise
    gray = cv2.medianBlur(gray, 3)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(th)


# """
# Enhanced OCR Preprocessor for Form 15CB Documents

# This module significantly improves OCR accuracy by applying multiple
# image enhancement techniques before passing to Tesseract.
# """

# import cv2
# import numpy as np
# from PIL import Image
# import logging

# logger = logging.getLogger(__name__)


# def enhance_image_for_ocr(pil_image, debug=False):
#     """
#     Apply comprehensive image enhancements for better OCR results.
    
#     Techniques applied:
#     1. Grayscale conversion
#     2. Noise reduction
#     3. Contrast enhancement (CLAHE)
#     4. Adaptive thresholding
#     5. Deskewing
#     6. Resolution upscaling
    
#     Args:
#         pil_image: PIL Image object
#         debug: If True, save intermediate images for inspection
        
#     Returns:
#         Enhanced PIL Image ready for OCR
#     """
    
#     logger.info("🔧 Applying image enhancements for better OCR...")
    
#     try:
#         # Convert PIL to OpenCV format
#         img_array = np.array(pil_image.convert('RGB'))
#         img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
        
#         # Step 1: Convert to grayscale
#         gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
#         logger.debug("  ✓ Converted to grayscale")
        
#         # Step 2: Upscale if resolution is low
#         h, w = gray.shape
#         target_width = 3000  # Higher resolution for better OCR
#         if w < target_width:
#             scale_factor = target_width / w
#             new_w = int(w * scale_factor)
#             new_h = int(h * scale_factor)
#             gray = cv2.resize(
#                 gray,
#                 (new_w, new_h),
#                 interpolation=cv2.INTER_CUBIC  # Better quality than LINEAR
#             )
#             logger.debug(f"  ✓ Upscaled from {w}x{h} to {new_w}x{new_h}")
        
#         # Step 3: Denoise
#         denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
#         logger.debug("  ✓ Applied denoising")
        
#         # Step 4: Enhance contrast using CLAHE
#         clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
#         enhanced = clahe.apply(denoised)
#         logger.debug("  ✓ Enhanced contrast (CLAHE)")
        
#         # Step 5: Sharpen the image
#         kernel_sharpen = np.array([
#             [-1, -1, -1],
#             [-1,  9, -1],
#             [-1, -1, -1]
#         ])
#         sharpened = cv2.filter2D(enhanced, -1, kernel_sharpen)
#         logger.debug("  ✓ Sharpened image")
        
#         # Step 6: Adaptive thresholding (works better than simple threshold)
#         binary = cv2.adaptiveThreshold(
#             sharpened,
#             255,
#             cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
#             cv2.THRESH_BINARY,
#             11,
#             2
#         )
#         logger.debug("  ✓ Applied adaptive thresholding")
        
#         # Step 7: Morphological operations to clean up
#         kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 1))
#         cleaned = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
#         logger.debug("  ✓ Morphological cleanup")
        
#         # Step 8: Deskew if needed
#         coords = np.column_stack(np.where(cleaned > 0))
#         if len(coords) > 0:
#             angle = cv2.minAreaRect(coords)[-1]
#             if angle < -45:
#                 angle = -(90 + angle)
#             else:
#                 angle = -angle
            
#             # Only deskew if angle is significant
#             if abs(angle) > 0.5:
#                 (h, w) = cleaned.shape[:2]
#                 center = (w // 2, h // 2)
#                 M = cv2.getRotationMatrix2D(center, angle, 1.0)
#                 cleaned = cv2.warpAffine(
#                     cleaned,
#                     M,
#                     (w, h),
#                     flags=cv2.INTER_CUBIC,
#                     borderMode=cv2.BORDER_REPLICATE
#                 )
#                 logger.debug(f"  ✓ Deskewed by {angle:.2f} degrees")
        
#         # Save debug images if requested
#         if debug:
#             cv2.imwrite('debug_1_original_gray.png', gray)
#             cv2.imwrite('debug_2_denoised.png', denoised)
#             cv2.imwrite('debug_3_enhanced.png', enhanced)
#             cv2.imwrite('debug_4_sharpened.png', sharpened)
#             cv2.imwrite('debug_5_binary.png', binary)
#             cv2.imwrite('debug_6_final.png', cleaned)
#             logger.info("  📁 Debug images saved")
        
#         # Convert back to PIL Image
#         result = Image.fromarray(cleaned)
        
#         logger.info("✅ Image enhancement complete!")
#         return result
        
#     except Exception as e:
#         logger.error(f"❌ Image enhancement failed: {e}")
#         logger.warning("  Falling back to original image")
#         return pil_image


# def preprocess_invoice_image(pil_image):
#     """
#     Specialized preprocessing for invoice/form documents.
#     Optimized for Form 15CB structure.
#     """
    
#     try:
#         img_array = np.array(pil_image.convert('RGB'))
#         img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
#         gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
#         # Very aggressive upscaling for invoices
#         h, w = gray.shape
#         if w < 2400:
#             scale = 2400 / w
#             gray = cv2.resize(
#                 gray,
#                 (int(w * scale), int(h * scale)),
#                 interpolation=cv2.INTER_CUBIC
#             )
        
#         # Strong denoising for invoices
#         denoised = cv2.bilateralFilter(gray, 9, 75, 75)
        
#         # Otsu's thresholding (works well for text)
#         _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
#         # Invert if background is dark
#         if np.mean(binary) < 127:
#             binary = cv2.bitwise_not(binary)
        
#         return Image.fromarray(binary)
        
#     except Exception as e:
#         logger.error(f"Invoice preprocessing failed: {e}")
#         return pil_image


# def enhance_pdf_page_for_ocr(pdf_page_image):
#     """
#     Enhancement specifically for PDF pages converted to images.
#     """
    
#     try:
#         # PDF pages are usually cleaner, so lighter processing
#         img_array = np.array(pdf_page_image.convert('L'))  # Grayscale
        
#         # Upscale
#         h, w = img_array.shape
#         if w < 2000:
#             scale = 2000 / w
#             img_array = cv2.resize(
#                 img_array,
#                 (int(w * scale), int(h * scale)),
#                 interpolation=cv2.INTER_LINEAR
#             )
        
#         # Light denoise
#         denoised = cv2.fastNlMeansDenoising(img_array, None, h=7)
        
#         # Adaptive threshold
#         binary = cv2.adaptiveThreshold(
#             denoised,
#             255,
#             cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
#             cv2.THRESH_BINARY,
#             15,
#             3
#         )
        
#         return Image.fromarray(binary)
        
#     except Exception as e:
#         logger.error(f"PDF enhancement failed: {e}")
#         return pdf_page_image


# # For testing
# if __name__ == "__main__":
#     import sys
#     logging.basicConfig(level=logging.INFO)
    
#     if len(sys.argv) > 1:
#         # Test with an image file
#         image_path = sys.argv[1]
#         img = Image.open(image_path)
        
#         print(f"\nProcessing: {image_path}")
#         print(f"Original size: {img.size}")
        
#         # Apply enhancement
#         enhanced = enhance_image_for_ocr(img, debug=True)
        
#         print(f"Enhanced size: {enhanced.size}")
#         print("✅ Enhancement complete!")
#         print("🔍 Check debug_*.png files to see the transformations")
        
#         # Test OCR
#         try:
#             import pytesseract
#             print("\n" + "=" * 60)
#             print("OCR TEST")
#             print("=" * 60)
            
#             print("\nOriginal image OCR:")
#             original_text = pytesseract.image_to_string(img)
#             print(f"Characters extracted: {len(original_text)}")
#             print(original_text[:500])
            
#             print("\n" + "-" * 60)
#             print("Enhanced image OCR:")
#             enhanced_text = pytesseract.image_to_string(enhanced)
#             print(f"Characters extracted: {len(enhanced_text)}")
#             print(enhanced_text[:500])
            
#             improvement = len(enhanced_text) - len(original_text)
#             print("\n" + "=" * 60)
#             print(f"Improvement: +{improvement} characters ({improvement/len(original_text)*100:.1f}% more)")
#             print("=" * 60)
            
#         except ImportError:
#             print("\n⚠️ Pytesseract not available for testing")
#     else:
#         print("Usage: python preprocessor.py <image_file>")
#         print("Example: python preprocessor.py invoice.jpg")