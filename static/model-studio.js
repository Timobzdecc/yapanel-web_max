// Station Max 3D studio widget (Three.js + OrbitControls).
import * as T from 'three';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import { modelSettings } from './model-settings.js';
import { CONCEPTS } from './model-concepts.js';

export function createModel(profile) {
  const view = document.getElementById('model-view');
  const concept = CONCEPTS[profile?.id] || CONCEPTS.max;
  view.setAttribute('role', 'group');
  view.setAttribute('aria-label', '3D-модель: вращайте мышью или пальцем');
  view.classList.add('studio-view');

  const palette = concept.colors;
  let renderer;
  try {
    renderer = new T.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
  } catch (e) {
    view.insertAdjacentHTML('beforeend', '<p class="studio-error">Для объёмной модели включите WebGL в браузере.</p>');
    throw e;
  }
  renderer.domElement.className = 'studio-canvas';
  renderer.domElement.setAttribute('aria-label', (profile?.name || 'Станция Макс') + ' — вращаемая 3D-модель');
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = T.SRGBColorSpace;
  renderer.toneMapping = T.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  view.appendChild(renderer.domElement);

  const note = document.createElement('p');
  note.className = 'studio-caption';
  note.hidden = true;
  view.append(note);

  let dirty = true;
  let failed = false;
  renderer.domElement.addEventListener('webglcontextlost', e => {
    e.preventDefault();
    note.hidden = false;
    note.textContent = '3D-модель перезапускается…';
  });
  renderer.domElement.addEventListener('webglcontextrestored', () => {
    note.hidden = true;
    failed = false;
    dirty = true;
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) dirty = true; });
  window.addEventListener('focus', () => { dirty = true; });
  window.addEventListener('pageshow', () => { dirty = true; });

  const scene = new T.Scene();
  const camera = new T.PerspectiveCamera(30, 1, 1, 2500);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.enablePan = false;
  controls.minDistance = 240;
  controls.maxDistance = 820;
  controls.maxPolarAngle = Math.PI * 0.91;
  controls.autoRotateSpeed = 0.7;
  controls.enabled = true;
  renderer.domElement.style.touchAction = 'none';

  const group = new T.Group();
  scene.add(group);
  scene.add(new T.HemisphereLight(0xf1f4ff, 0x45404b, 2));
  const keyLight = new T.DirectionalLight(0xfff5e9, 3.2);
  keyLight.position.set(-140, 220, 180);
  scene.add(keyLight);
  const fill = new T.DirectionalLight(0xd7e4ff, 1.5);
  fill.position.set(160, 80, -100);
  scene.add(fill);

  function canvasTexture(size, draw) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    draw(canvas.getContext('2d'), size);
    const texture = new T.CanvasTexture(canvas);
    texture.colorSpace = T.SRGBColorSpace;
    return texture;
  }

  const fallback = Object.keys(palette)[0];
  const fabric = new T.MeshStandardMaterial({ color: palette[fallback][1], roughness: 0.92, metalness: 0.02 });
  const plastic = new T.MeshStandardMaterial({ color: '#27282d', roughness: 0.55, metalness: 0.04 });
  const rubber = new T.MeshStandardMaterial({ color: '#101114', roughness: 0.85 });
  const glass = new T.MeshStandardMaterial({ color: '#303139', roughness: 0.35, metalness: 0.1 });

  function mesh(geo, mat, x = 0, y = 0, z = 0, parent = group) {
    const m = new T.Mesh(geo, mat);
    m.position.set(x, y, z);
    parent.add(m);
    return m;
  }
  function roundedShape(w, d, r) {
    const s = new T.Shape();
    const x = -w / 2;
    const z = -d / 2;
    s.moveTo(x + r, z);
    s.lineTo(x + w - r, z);
    s.quadraticCurveTo(x + w, z, x + w, z + r);
    s.lineTo(x + w, z + d - r);
    s.quadraticCurveTo(x + w, z + d, x + w - r, z + d);
    s.lineTo(x + r, z + d);
    s.quadraticCurveTo(x, z + d, x, z + d - r);
    s.lineTo(x, z + r);
    s.quadraticCurveTo(x, z, x + r, z);
    return s;
  }
  function roundedBox(w, h, d, r, b = 1) {
    const geo = new T.ExtrudeGeometry(roundedShape(w - 2 * b, d - 2 * b, Math.max(1, r - b)), {
      depth: h - 2 * b,
      bevelEnabled: true,
      bevelThickness: b,
      bevelSize: b,
      bevelSegments: 5,
      steps: 1,
      curveSegments: 16,
    });
    geo.rotateX(-Math.PI / 2);
    geo.translate(0, -h / 2 + b, 0);
    geo.computeVertexNormals();
    return geo;
  }

  const ledMaterials = [];
  function ledMaterial() {
    const m = new T.MeshStandardMaterial({
      color: '#181820',
      emissive: '#000000',
      emissiveIntensity: 0,
      roughness: 0.28,
      metalness: 0.05,
    });
    ledMaterials.push(m);
    return m;
  }

  const details = concept.build({ T, group, mesh, roundedBox, canvasTexture, fabric, plastic, rubber, glass, ledMaterial, renderer, scene });
  const display = details.display || null;

  window.modelShow = values => {
    if (display) display(values);
    dirty = true;
  };
  window.modelRing = leds => {
    if (!leds?.length) return;
    if (details.ring) {
      details.ring(leds);
      dirty = true;
      return;
    }
    ledMaterials.forEach((m, i) => {
      const rgb = leds[Math.floor(i * leds.length / ledMaterials.length)] || [0, 0, 0];
      const color = new T.Color().setRGB(...rgb.map(v => Math.max(0, Math.min(255, v)) / 255), T.SRGBColorSpace);
      m.color.copy(color);
      m.emissive.copy(color);
      m.emissiveIntensity = 0.55;
    });
    dirty = true;
  };

  function paint(key) {
    key = palette[key] ? key : fallback;
    const [, body, trim] = palette[key];
    fabric.color.set(body);
    if (trim) plastic.color.set(trim);
    else plastic.color.copy(fabric.color).multiplyScalar(0.65);
    dirty = true;
  }
  let preference = null;
  window.modelColor = key => { if (!preference) paint(key); };
  paint(window.__deviceColor || fallback);

  const bounds = new T.Box3().setFromObject(group).getBoundingSphere(new T.Sphere());
  controls.minDistance = bounds.radius * 1.6;
  controls.maxDistance = bounds.radius * 6;
  controls.maxPolarAngle = Math.PI;

  function home() {
    camera.position.copy(bounds.center).add(new T.Vector3(0.38, 0.42, 1).setLength(bounds.radius * 4.3));
    controls.target.copy(bounds.center);
    controls.update();
    dirty = true;
  }
  home();

  modelSettings(profile?.id || 'max', palette, {
    color: key => {
      preference = key;
      paint(key || window.__deviceColor || fallback);
    },
    home,
    spin: value => {
      controls.autoRotate = value;
      dirty = true;
    },
  });
  controls.addEventListener('change', () => { dirty = true; });
  controls.addEventListener('start', () => { view.classList.add('studio-used'); });

  let visible = true;
  new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    dirty = true;
  }).observe(view);
  new ResizeObserver(() => {
    const { width, height } = view.getBoundingClientRect();
    if (width && height) {
      renderer.setSize(width, height);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      dirty = true;
    }
  }).observe(view);

  function frame() {
    if (failed) return;
    requestAnimationFrame(frame);
    if (document.hidden || !visible) return;
    controls.update();
    if (dirty || controls.autoRotate) {
      try {
        renderer.render(scene, camera);
      } catch (e) {
        failed = true;
        note.hidden = false;
        note.textContent = '3D-модель не отрисовывается — обновите страницу.';
        console.error('3D-модель:', e);
        return;
      }
      dirty = false;
      view.dataset.renderer = 'webgl';
      view.dataset.model = profile?.id || 'max';
    }
  }
  requestAnimationFrame(frame);
}
