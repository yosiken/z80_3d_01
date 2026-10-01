#!/usr/bin/env python3
"""Wavefront OBJ -> demo object data.

Used by tools/gen_tables.py for every models/*.obj, or standalone to check a
model:   python3 tools/obj2inc.py models/ship.obj

Conversion:
  * only 'v' and 'f' records are used (UVs, normals, materials are ignored)
  * axes: OBJ is right handed (+Y up, +Z towards the viewer); the demo uses
    +Y up, +Z away from the viewer, so Z is negated. Face winding must be
    counter-clockwise seen from outside (the OBJ/Blender default).
  * the model is centred on its bounding box and scaled so that the farthest
    vertex is RADIUS units away (keeps the projection inside 128x128)
  * coordinates are rounded to 8 bit, identical vertices are merged,
    degenerate faces are dropped
  * concave polygons are split into triangles (ear clipping); wireframe edges
    always come from the original polygons

There is no depth sorting: hidden-line / shaded modes are only correct for
convex models. Wireframe works for any model.
"""
import math
import os
import sys

RADIUS = 54
NORMAL_SCALE = 64
MAX_VERTICES = 128
MAX_COUNT = 255          # edges / faces per object (stored as bytes)


class ModelError(Exception):
    pass


def sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def newell(pts):
    """Polygon normal (not normalised), direction = (v1-v0)x(v2-v0) for a convex CCW polygon."""
    n = [0.0, 0.0, 0.0]
    for i in range(len(pts)):
        a = pts[i]
        b = pts[(i + 1) % len(pts)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    return n


def read_obj(path):
    verts = []
    faces = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'v':
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif parts[0] == 'f':
                idx = []
                for p in parts[1:]:
                    i = int(p.split('/')[0])
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                faces.append(idx)
    if not verts or not faces:
        raise ModelError("%s: no vertices/faces" % path)
    return verts, faces


def project_2d(pts, n):
    """Project 3D polygon points onto the plane with normal n -> 2D, same winding."""
    ax = max(range(3), key=lambda i: abs(n[i]))
    u, v = [(1, 2), (2, 0), (0, 1)][ax]
    if n[ax] < 0:
        u, v = v, u
    return [(p[u], p[v]) for p in pts]


def is_convex(p2):
    n = len(p2)
    for i in range(n):
        a, b, c = p2[i], p2[(i + 1) % n], p2[(i + 2) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) < -1e-9:
            return False
    return True


def ear_clip(idx, p2):
    """Triangulate a simple CCW polygon. Returns list of index triples."""
    def area(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def inside(p, a, b, c):
        return area(a, b, p) >= 0 and area(b, c, p) >= 0 and area(c, a, p) >= 0

    ring = list(range(len(idx)))
    tris = []
    guard = 0
    while len(ring) > 3 and guard < 10000:
        guard += 1
        for k in range(len(ring)):
            i0, i1, i2 = ring[k - 1], ring[k], ring[(k + 1) % len(ring)]
            a, b, c = p2[i0], p2[i1], p2[i2]
            if area(a, b, c) <= 1e-12:
                continue
            if any(inside(p2[j], a, b, c) for j in ring if j not in (i0, i1, i2)):
                continue
            tris.append([idx[i0], idx[i1], idx[i2]])
            ring.pop(k)
            break
        else:
            break   # not simple: fall back to a fan for the rest
    for k in range(1, len(ring) - 1):
        tris.append([idx[ring[0]], idx[ring[k]], idx[ring[k + 1]]])
    return tris


def convert(path):
    """Returns (int vertices, edges, faces[(indices, int normal)], stats dict)."""
    raw_v, raw_f = read_obj(path)
    # axes: negate Z
    raw_v = [[x, y, -z] for x, y, z in raw_v]
    lo = [min(v[i] for v in raw_v) for i in range(3)]
    hi = [max(v[i] for v in raw_v) for i in range(3)]
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    used = sorted({i for f in raw_f for i in f})
    r = max(math.sqrt(dot(sub(raw_v[i], centre), sub(raw_v[i], centre))) for i in used)
    if r == 0:
        raise ModelError("%s: model has no extent" % path)
    scale = RADIUS / r
    fv = [[(c - centre[k]) * scale for k, c in enumerate(v)] for v in raw_v]

    # quantise + merge identical vertices
    key_to_new = {}
    new_v = []
    remap = {}
    for i in used:
        q = tuple(int(round(c)) for c in fv[i])
        if q not in key_to_new:
            key_to_new[q] = len(new_v)
            new_v.append(list(q))
        remap[i] = key_to_new[q]
    if len(new_v) > MAX_VERTICES:
        raise ModelError("%s: %d vertices (max %d) - reduce the polygon count" %
                         (path, len(new_v), MAX_VERTICES))

    edges = set()
    faces = []
    dropped = 0
    split = 0
    for f in raw_f:
        idx = []
        for i in f:
            j = remap[i]
            if not idx or idx[-1] != j:
                idx.append(j)
        while len(idx) > 1 and idx[0] == idx[-1]:
            idx.pop()
        if len(idx) < 3:
            dropped += 1
            continue
        pts = [new_v[i] for i in idx]
        nrm = newell([fv_pt for fv_pt in pts])
        ln = math.sqrt(dot(nrm, nrm))
        if ln < 1e-6:
            dropped += 1
            continue
        for a, b in zip(idx, idx[1:] + idx[:1]):
            edges.add((min(a, b), max(a, b)))
        p2 = project_2d(pts, nrm)
        polys = [idx] if (len(idx) == 3 or is_convex(p2)) else ear_clip(idx, p2)
        if len(polys) > 1:
            split += 1
        # outward normal: the demo stores faces so that (v1-v0)x(v2-v0)
        # points INTO the object; OBJ CCW + negated Z already gives that
        outward = [-c / ln for c in nrm]
        nq = [int(round(c * NORMAL_SCALE)) for c in outward]
        for poly in polys:
            # start at the corner with the largest triangle so that the
            # visibility test (first three vertices) is robust
            best = max(range(len(poly)), key=lambda k: abs(dot(
                cross(sub(new_v[poly[(k + 1) % len(poly)]], new_v[poly[k]]),
                      sub(new_v[poly[(k + 2) % len(poly)]], new_v[poly[k]])),
                cross(sub(new_v[poly[(k + 1) % len(poly)]], new_v[poly[k]]),
                      sub(new_v[poly[(k + 2) % len(poly)]], new_v[poly[k]])))))
            poly = poly[best:] + poly[:best]
            faces.append((poly, nq))

    edges = sorted(edges)
    if len(edges) > MAX_COUNT or len(faces) > MAX_COUNT:
        raise ModelError("%s: %d edges / %d faces (max %d each) - reduce the polygon count" %
                         (path, len(edges), len(faces), MAX_COUNT))
    stats = {'dropped': dropped, 'split': split, 'source_vertices': len(raw_v),
             'source_faces': len(raw_f)}
    return new_v, edges, faces, stats


def name_from_path(path):
    base = os.path.splitext(os.path.basename(path))[0].upper()
    ok = ''.join(c if (c.isascii() and (c.isalnum() or c in ' +-.')) else ' ' for c in base)
    return ok.strip()[:12] or 'MODEL'


def label_from_path(path):
    base = os.path.splitext(os.path.basename(path))[0].lower()
    return 'model_' + ''.join(c if (c.isascii() and c.isalnum()) else '_' for c in base)


def main():
    for path in sys.argv[1:]:
        try:
            v, e, f, st = convert(path)
        except ModelError as ex:
            print("error:", ex)
            continue
        print("%s: %d vertices, %d edges, %d faces (source %d v / %d f, %d dropped, %d split)" %
              (path, len(v), len(e), len(f), st['source_vertices'], st['source_faces'],
               st['dropped'], st['split']))


if __name__ == '__main__':
    main()
