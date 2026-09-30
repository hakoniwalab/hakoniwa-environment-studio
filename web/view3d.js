// 3D preview: the GLB tools/env_generate.py makes for the Recipe being edited
// (POST /api/glb), in three.js. It follows the 2D edits; it does not edit.
// glTF axes: x = East, y = Up, z = -North; the origin is the environment's
// centre, as in the Recipe.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const CAR_EYE_M = 1.2; // a driver's eye height
const HIGHLIGHT = new THREE.Color(0x3a6fd8);
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
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  resize() {
    const { clientWidth: width, clientHeight: height } = this.container;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  // Replace the environment with a new GLB; the camera stays unless the size changed.
  async setGlb(buffer, size) {
    const gltf = await this.loader.parseAsync(buffer, "");
    // Objects of one colour share a material in the GLB; give each its own so
    // highlighting one does not light up the others.
    gltf.scene.traverse((node) => { if (node.isMesh) node.material = node.material.clone(); });
    this.model.clear();
    this.model.add(gltf.scene);
    const resized = !this.size || this.size.east !== size.east || this.size.north !== size.north;
    this.size = { ...size };
    this.applyHighlight();
    if (resized) this.overview();
  }

  // ids: the selected objects (one or several).
  setSelected(ids) {
    this.selected = new Set(ids || []);
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

  // A driver at the south edge, eye 1.2 m above the ground there, looking north.
  carView() {
    if (!this.size) return;
    const z = this.size.north / 2 - 1;
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
