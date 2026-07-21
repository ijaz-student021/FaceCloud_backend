import cv2


class PerceptualFaceRegionProvider:
    """Model-free duplicate-face evidence; this is not identity recognition."""

    name = "perceptual_face_region_v1"

    def __init__(self, hash_function):
        self.hash_function = hash_function

    def fingerprints(self, image, faces):
        image_height, image_width = image.shape[:2]
        fingerprints = []
        for index, face in enumerate(faces):
            try:
                x = int(face["x"])
                y = int(face["y"])
                width = int(face["width"])
                height = int(face["height"])
            except (KeyError, TypeError, ValueError):
                continue
            if width <= 0 or height <= 0:
                continue
            margin_x = max(2, round(width * 0.08))
            margin_y = max(2, round(height * 0.08))
            left = max(0, x - margin_x)
            top = max(0, y - margin_y)
            right = min(image_width, x + width + margin_x)
            bottom = min(image_height, y + height + margin_y)
            if right - left < 8 or bottom - top < 8:
                continue
            region = image[top:bottom, left:right]
            if region.size == 0:
                continue
            normalized = cv2.resize(region, (128, 128), interpolation=cv2.INTER_AREA)
            fingerprints.append(
                {
                    "face_index": index,
                    "fingerprint": self.hash_function(normalized),
                }
            )
        return fingerprints


def get_face_fingerprint_provider(hash_function):
    """Provider boundary for a future commercially licensed embedding model."""

    return PerceptualFaceRegionProvider(hash_function)
