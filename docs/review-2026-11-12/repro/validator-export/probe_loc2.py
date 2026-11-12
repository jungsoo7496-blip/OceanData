import json, sys
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services.contracts import validate_document
m = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))
def show(label, loc):
    probs, _ = validate_document("upload-manifest", {**m, "location": loc})
    print(f"{label:55s}", [(p.pointer, p.code) for p in probs])
show("Point (0,0) + extra property 'bbox'", {"type": "Point", "coordinates": [0, 0], "bbox": [0, 0, 1, 1]})
show("same, null-island fixed -> (126.5,35.1)", {"type": "Point", "coordinates": [126.5, 35.1], "bbox": [0, 0, 1, 1]})
show("Polygon, 1 ring of 3 positions + swapped position", {"type": "Polygon", "coordinates": [[[126, 35], [127, 100], [128, 36]]]})
show("Point [10,200,'abc'] (swapped + bad 3rd element)", {"type": "Point", "coordinates": [10, 200, "abc"]})
show("Point [10,20,'abc'] only bad 3rd element", {"type": "Point", "coordinates": [10, 20, "abc"]})
