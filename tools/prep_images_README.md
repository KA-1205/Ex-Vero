# Image seed preparation

`prep_images.py` converts a Roboflow YOLOv8 export into the Ex-Vero image
seed. It is offline after the export is downloaded:

```powershell
python tools/prep_images.py data/server_room --output config/seed
```

The script keeps four deterministic examples each of `fire`, `smoke`, and
`none`. An empty YOLO annotation file is the source of a `none` example. It
resizes images to a maximum 1024px side, writes JPEGs, and generates both JSONL
manifests. The conflict file deliberately reuses `cam-01`, `cam-02`, and
`cam-03`; `cam-03` is the frequent outlier so trust decay can be exercised.

## Source and license

Images and labels come from Roboflow Universe, **ServeRoom fire and smoke dtc.**,
dataset version 9:

<https://universe.roboflow.com/server-room-fire-and-smoke-detection/serveroom-fire-and-smoke-dtc./dataset/9>

The project declares **CC BY 4.0**. Preserve that attribution when redistributing
the seed. The source export has `fire` and `smoke` classes; its empty annotation
files are used as `none` negatives. The checked-in conflict scenarios are
evaluation fixtures that assign those images to different camera reports; they
do not claim the source dataset contains synchronized multi-camera captures.
