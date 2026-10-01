// 3D preview: the GLB tools/env_generate.py makes for the Recipe being edited
// (POST /api/glb), in three.js. It follows the 2D edits; it does not edit.
// glTF axes: x = East, y = Up, z = -North; the origin is the environment's
// centre, as in the Recipe.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const CAR_EYE_M = 1.2; // a driver's eye height
// A press that moves less than this and is let go soon is a click (picks a
// part); anything else turns the camera.
const CLICK_SLOP_PX = 5;
const CLICK_MS = 500;
const HIGHLIGHT = new THREE.Color(0x3a6fd8);

// Free what a model holds on the GPU (geometries, materials, their textures).
function dispose(root) {
  root.traverse((node) => {
    if (!node.isMesh) return;
    node.geometry?.dispose();
    for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
      for (const value of Object.values(material || {})) if (value?.isTexture) value.dispose();
      material?.dispose();
    }
  });
}
const BLACK = new THREE.Color(0x000000);

export class View3D {
  constructor(container) {
    this.container = container;
    this.loader = new GLTFLoader();
    this.size = null; // {east, north} in metres
    this.selected = new Set();
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    container.append(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xcfe0ee); // sky
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x6f7a66, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-30, 60, 20);
    this.scene.add(sun);
    this.model = new THREE.Group();
    this.scene.add(this.model);
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.05, 5000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI / 2 - 0.02; // stay above the ground
    // Which way the view looks: a compass that turns with the camera (a click
    // turns the camera to look north).
    this.lastCamera = "";
    this.compass = this.makeCompass();
    container.append(this.compass);
    // A click picks the part under the pointer: onSelect(id or null, additive).
    this.onSelect = null;
    this.raycaster = new THREE.Raycaster();
    const canvas = this.renderer.domElement;
    canvas.addEventListener("pointerdown", (event) => {
      this.glide = null;  // the user takes the camera
      this.press = event.button === 0 ? { x: event.clientX, y: event.clientY, time: performance.now() } : null;
    });
    canvas.addEventListener("pointerup", (event) => {
      const press = this.press;
      this.press = null;
      if (!press || !this.onSelect || performance.now() - press.time > CLICK_MS
          || Math.hypot(event.clientX - press.x, event.clientY - press.y) > CLICK_SLOP_PX) return;
      this.onSelect(this.partAt(event), event.shiftKey || event.metaKey || event.ctrlKey);
    });
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    this.setActive(true);
  }

  makeCompass() {
    const ns = "http://www.w3.org/2000/svg";
    const make = (tag, attributes) => {
      const node = document.createElementNS(ns, tag);
      for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
      return node;
    };
    const button = document.createElement("button");
    button.type = "button";
    button.className = "compass";
    button.title = "方位（赤が北）。クリックで北を奥にして見ます";
    const icon = make("svg", { viewBox: "-20 -20 40 40", width: "44", height: "44", "aria-hidden": "true" });
    icon.append(make("circle", { r: "19", class: "compass-dial" }));
    this.needle = make("g", {});
    this.needle.append(make("path", { d: "M0,-10 L4.5,0 L-4.5,0 Z", class: "compass-north" }),
      make("path", { d: "M0,10 L4.5,0 L-4.5,0 Z", class: "compass-south" }));
    const label = make("text", { y: "-11.5", "text-anchor": "middle", class: "compass-label" });
    label.textContent = "N";
    this.needle.append(label);
    icon.append(this.needle);
    button.append(icon);
    button.addEventListener("click", () => this.faceNorth());
    return button;
  }

  // Turn the compass when the camera moved: to where north (glTF -z) from
  // the point looked at appears on the screen (right looking down too).
  cameraMoved() {
    const { position } = this.camera;
    const target = this.controls.target;
    const key = [position.x, position.y, position.z, target.x, target.y, target.z, this.camera.aspect]
      .map((value) => value.toFixed(3)).join(",");
    if (key === this.lastCamera) return;
    this.lastCamera = key;
    const from = target.clone().project(this.camera);
    const to = target.clone().add(new THREE.Vector3(0, 0, -1)).project(this.camera);
    const screen = Math.atan2(to.x - from.x, to.y - from.y) * 180 / Math.PI; // clockwise from up
    if (Number.isFinite(screen)) this.needle.setAttribute("transform", `rotate(${screen.toFixed(1)})`);
  }

  // A small picture of one object as a PNG data URL (for a Catalog item):
  // seen from the south-west, above, on a clear background. Null when it is not in the model.
  snapshot(id, width = 160, height = 120) {
    const node = this.nodes?.get(id);
    if (!node) return null;
    const copy = node.clone(true);
    copy.traverse((child) => {
      if (!child.isMesh) return;
      child.material = Array.isArray(child.material) ? child.material.map((m) => m.clone()) : child.material.clone();
      for (const material of Array.isArray(child.material) ? child.material : [child.material]) {
        material.emissive?.copy(BLACK);  // not the selection's highlight
      }
    });
    copy.position.set(0, 0, 0);
    copy.rotation.set(0, 0, 0);
    const scene = new THREE.Scene();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x6f7a66, 1.8));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-30, 60, 20);
    scene.add(sun, copy);
    const sphere = new THREE.Box3().setFromObject(copy).getBoundingSphere(new THREE.Sphere());
    const camera = new THREE.PerspectiveCamera(35, width / height, 0.05, 10000);
    const distance = Math.max(sphere.radius, 0.3) / Math.sin((35 * Math.PI) / 360) * 1.1;
    camera.position.copy(sphere.center).add(new THREE.Vector3(-0.6, 0.55, 0.6).normalize().multiplyScalar(distance));
    camera.lookAt(sphere.center);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    try {
      renderer.setSize(width, height, false);
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.setClearColor(0x000000, 0);
      renderer.render(scene, camera);
      return renderer.domElement.toDataURL("image/png");
    } finally {
      copy.traverse((child) => {
        if (child.isMesh) for (const m of Array.isArray(child.material) ? child.material : [child.material]) m.dispose();
      });
      renderer.dispose();
      renderer.forceContextLoss();
    }
  }

  // Bring the camera to objects (ids), keeping the way it looks: the point
  // looked at moves to their centre and the distance fits their size, in a
  // short glide. False when none of them is in the model yet.
  focus(ids) {
    const box = new THREE.Box3();
    for (const id of ids) {
      const node = this.nodes?.get(id);
      if (node) box.expandByObject(node);
    }
    if (box.isEmpty()) return false;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const fit = Math.max(sphere.radius, 0.5) / Math.sin((this.camera.fov * Math.PI) / 360) * 1.3;
    const direction = this.camera.position.clone().sub(this.controls.target).normalize();
    this.glide = {
      start: performance.now(), duration: 450,
      from: { position: this.camera.position.clone(), target: this.controls.target.clone() },
      to: { position: sphere.center.clone().addScaledVector(direction, Math.max(fit, 3)), target: sphere.center.clone() },
    };
    return true;
  }

  stepGlide() {
    const glide = this.glide;
    if (!glide) return;
    const t = Math.min((performance.now() - glide.start) / glide.duration, 1);
    const eased = 1 - (1 - t) ** 3;
    this.camera.position.lerpVectors(glide.from.position, glide.to.position, eased);
    this.controls.target.lerpVectors(glide.from.target, glide.to.target, eased);
    if (t >= 1) this.glide = null;
  }

  // The id of the part nearest the camera under a pointer event, or null
  // (the sky, the terrain).
  partAt(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const pointer = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(pointer, this.camera);
    for (const hit of this.raycaster.intersectObject(this.model, true)) {
      for (let node = hit.object; node && node !== this.model; node = node.parent) {
        if (node.userData?.object) return node.userData.object;
        if (node.userData?.terrain) return null;  // what is behind the ground is out of sight
      }
    }
    return null;
  }

  // Look north (from the south of the point looked at), keeping the distance and the height.
  faceNorth() {
    const target = this.controls.target;
    const offset = this.camera.position.clone().sub(target);
    const flat = Math.hypot(offset.x, offset.z);
    this.look(new THREE.Vector3(target.x, this.camera.position.y, target.z + Math.max(flat, 0.01)), target.clone());
  }

  resize() {
    const { clientWidth: width, clientHeight: height } = this.container;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  // Replace the environment with a new GLB; the camera stays unless the size
  // changed. current() says whether this GLB is still the newest one asked for.
  async setGlb(buffer, size, current = () => true) {
    const gltf = await this.loader.parseAsync(buffer, "");
    if (!current()) {
      dispose(gltf.scene);
      return;
    }
    // Objects of one colour share a material in the GLB; give each its own so
    // highlighting one does not light up the others.
    gltf.scene.traverse((node) => {
      if (node.isMesh) node.material = Array.isArray(node.material) ? node.material.map((m) => m.clone()) : node.material.clone();
    });
    dispose(this.model); // the previous environment's GPU buffers, materials and textures
    this.model.clear();
    this.model.add(gltf.scene);
    // Each object is one node (extras.object = its id): moving it needs no new GLB.
    this.nodes = new Map();
    gltf.scene.traverse((node) => {
      const id = node.userData?.object;
      if (id && !this.nodes.has(id)) this.nodes.set(id, node);
    });
    const resized = !this.size || this.size.east !== size.east || this.size.north !== size.north;
    this.size = { ...size };
    this.applyHighlight();
    if (resized) this.overview();
  }

  // Move objects without a new GLB. poses: id -> {translation: [x, y, z]
  // (glTF axes), yaw_deg}; a missing translation[1] keeps the height shown.
  // Returns false when an object is not in the model (a new GLB is needed).
  setPoses(poses) {
    if (!this.nodes) return false;
    let all = true;
    for (const [id, pose] of Object.entries(poses)) {
      const node = this.nodes.get(id);
      if (!node) { all = false; continue; }
      const [x, y, z] = pose.translation;
      node.position.set(x, y ?? node.position.y, z);
      node.rotation.set(0, (pose.yaw_deg * Math.PI) / 180, 0);
    }
    return all;
  }

  // Render only while the 3D view is shown.
  setActive(active) {
    this.renderer.setAnimationLoop(active ? () => {
      this.stepGlide();
      this.controls.update();
      this.cameraMoved();
      this.renderer.render(this.scene, this.camera);
    } : null);
  }

  // ids: the selected objects (one or several).
  setSelected(ids) {
    const selected = new Set(ids || []);
    if (selected.size === this.selected.size && [...selected].every((id) => this.selected.has(id))) return;
    this.selected = selected;
    this.applyHighlight();
  }

  applyHighlight() {
    this.model.traverse((node) => {
      if (!node.isMesh) return;
      const selected = this.selected.has(node.name) || this.selected.has(node.parent?.name);
      node.material.emissive?.copy(selected ? HIGHLIGHT : BLACK);
      if (node.material.emissiveIntensity !== undefined) node.material.emissiveIntensity = selected ? 0.45 : 0;
    });
  }

  span() {
    return Math.max(this.size.east, this.size.north);
  }

  look(position, target) {
    this.glide = null;
    this.camera.position.copy(position);
    this.controls.target.copy(target);
    this.controls.update();
  }

  // From the south-west, above: the whole environment.
  overview() {
    if (!this.size) return;
    const span = this.span();
    this.look(new THREE.Vector3(-span * 0.3, span * 0.7, span * 0.95), new THREE.Vector3(0, 0, 0));
  }

  // The terrain's height at (x, z) in glTF axes: a ray down onto the "terrain" node.
  groundAt(x, z) {
    const terrain = this.model.getObjectByName("terrain");
    if (!terrain) return 0;
    const ray = new THREE.Raycaster(new THREE.Vector3(x, 10000, z), new THREE.Vector3(0, -1, 0));
    return ray.intersectObject(terrain, true)[0]?.point.y ?? 0;
  }

  // A driver at the south edge of the ground (an Envsim terrain may cover less
  // than the environment), eye 1.2 m above it, looking north.
  carView() {
    if (!this.size) return;
    const terrain = this.model.getObjectByName("terrain");
    const south = terrain ? new THREE.Box3().setFromObject(terrain).max.z : this.size.north / 2;
    const z = Math.min(this.size.north / 2, Number.isFinite(south) ? south : this.size.north / 2) - 1;
    const eye = this.groundAt(0, z) + CAR_EYE_M;
    this.look(new THREE.Vector3(0, eye, z), new THREE.Vector3(0, eye, z - this.size.north));
  }

  // A drone high above the south edge, looking down over the environment.
  droneView() {
    if (!this.size) return;
    const span = this.span();
    this.look(new THREE.Vector3(0, span * 0.45, this.size.north / 2 + span * 0.15), new THREE.Vector3(0, 0, -this.size.north * 0.1));
  }
}
