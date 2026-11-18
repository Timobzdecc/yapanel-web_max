// Station Max 3D body and colour palette (141 × 141 × 231 mm, 25 × 16 LED matrix, 24-LED top ring).
const pal=(...items)=>Object.fromEntries(items.map(([key,label,color])=>[key,[label,color]]));
// Colour names follow the official "Расцветки" lists on alice.yandex.ru/support.
const MAX=pal(['graphite','Графит','#34353b'],['beige','Бежевый','#cbbfad'],['green','Зелёный','#2f5a4b'],['turquoise','Бирюза','#3f8f8c'],['blue','Синий','#2f4a7a'],['bordo','Бордовый','#6b2834']);

function buildMax(kit) {
  const { T, group, mesh, roundedBox, fabric, plastic, rubber, glass, ledMaterial } = kit;
  const canvasTexture = kit.canvasTexture || ((size, draw) => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    draw(canvas.getContext('2d'), size);
    const tex = new T.CanvasTexture(canvas);
    tex.colorSpace = T.SRGBColorSpace;
    return tex;
  });

  // Official dimensions of Yandex Station Max: 141 x 141 x 231 mm
  const w = 141, d = 141, H = 231, r = 12;
  const capH = 10, baseH = 7;
  const h = H - capH - baseH; // 214 mm
  const y0 = (baseH - capH) / 2; // -1.5 mm
  const t = y0 + h / 2; // top of fabric body: 105.5 mm
  const b = y0 - h / 2; // bottom of fabric body: -108.5 mm

  // =========================================================================
  // 1. ДНО КОЛОНКИ (Bottom Base with regulatory plate & cable cutout)
  // =========================================================================
  // Main rubber base foot
  mesh(roundedBox(w - 2, baseH, d - 2, r - 1, 1), rubber, 0, b - baseH / 2);

  // Rear cable relief notch (at bottom back edge)
  const notchW = 44, notchH = baseH + 1, notchD = 14;
  mesh(new T.BoxGeometry(notchW, notchH, notchD), plastic, 0, b - baseH / 2 + 0.5, -d / 2 + notchD / 2 - 1);

  // Bottom plate graphic (Photo 1: Яндекс, Dolby Audio, EAC, Model info)
  const bottomTexture = canvasTexture(512, (ctx, n) => {
    ctx.fillStyle = '#18191c';
    ctx.fillRect(0, 0, n, n);

    // Silicone outer perimeter foot border
    ctx.strokeStyle = '#27282e';
    ctx.lineWidth = 14;
    ctx.strokeRect(36, 36, n - 72, n - 72);

    // Inner recessed plateau
    ctx.fillStyle = '#1c1d22';
    ctx.fillRect(50, 50, n - 100, n - 100);

    // "Яндекс" center logo
    ctx.fillStyle = '#c5c7ce';
    ctx.font = '600 36px "Yandex Sans Text", system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Яндекс', n / 2, n * 0.32);

    // Model description
    ctx.fillStyle = '#656872';
    ctx.font = '400 13px system-ui, sans-serif';
    ctx.fillText('Домашняя мультимедийная платформа', n / 2, n * 0.44);
    ctx.fillText('«Яндекс.Станция». Модель YNDX-00053.', n / 2, n * 0.49);
    ctx.fillText('20 В ⎓ 3.25 А · Сделано в Китае', n / 2, n * 0.54);

    // EAC and Dolby Audio logos
    ctx.fillStyle = '#9497a1';
    ctx.font = '700 24px system-ui, sans-serif';
    ctx.fillText('EAC', n * 0.34, n * 0.70);

    ctx.font = '600 20px system-ui, sans-serif';
    ctx.fillText('D◖Dolby Audio', n * 0.65, n * 0.70);

    // QR-code icon placeholder
    ctx.strokeStyle = '#4e515a';
    ctx.lineWidth = 2;
    ctx.strokeRect(n * 0.16, n * 0.64, 44, 44);
    ctx.fillStyle = '#60646e';
    ctx.fillRect(n * 0.16 + 8, n * 0.64 + 8, 12, 12);
    ctx.fillRect(n * 0.16 + 24, n * 0.64 + 24, 12, 12);
  });

  const bottomPlate = mesh(
    new T.PlaneGeometry(w - 14, d - 14),
    new T.MeshStandardMaterial({
      map: bottomTexture,
      roughness: 0.85,
      metalness: 0.05
    }),
    0, b - baseH - 0.1
  );
  bottomPlate.rotation.x = Math.PI / 2; // Facing down

  // =========================================================================
  // 4. ЗАД КОЛОНКИ (Back: Extruded Aluminum Radiator / Heatsink & Port Panel)
  // =========================================================================
  const spineW = 42;
  const spineH = h - 1;
  const spineD = 5.5;
  const spineZ = -d / 2 + spineD / 2 - 0.4;

  // Dark anodized metal for heatsink fins
  const metalRadiator = new T.MeshStandardMaterial({
    color: '#28292e',
    metalness: 0.78,
    roughness: 0.32
  });

  // Radiator base plate
  mesh(new T.BoxGeometry(spineW, spineH, spineD), metalRadiator, 0, y0, spineZ);

  // Vertical cooling fins (Photo 4: 11 prominent vertical fins)
  const finCount = 11;
  const finSpacing = (spineW - 6) / (finCount - 1); // ~3.6 mm spacing
  const finH = spineH * 0.75;
  const finY = y0 + (spineH - finH) / 2 - 2;

  for (let i = 0; i < finCount; i++) {
    const finX = -spineW / 2 + 3 + i * finSpacing;
    mesh(new T.BoxGeometry(1.6, finH, 4.2), metalRadiator, finX, finY, spineZ - spineD / 2 - 2.1);
  }

  // Rear Connector / Port Panel (Bottom section below heatsink)
  const portPanelH = spineH * 0.22;
  const portPanelY = y0 - (spineH - portPanelH) / 2 + 1;
  const portMat = new T.MeshStandardMaterial({ color: '#16171a', roughness: 0.7, metalness: 0.2 });
  mesh(new T.BoxGeometry(spineW - 2, portPanelH, spineD + 1), portMat, 0, portPanelY, spineZ - 0.5);

  // Ports: AUX (3.5mm), HDMI, Power barrel
  const jackMat = new T.MeshStandardMaterial({ color: '#52555f', metalness: 0.8, roughness: 0.4 });
  const holeMat = new T.MeshBasicMaterial({ color: '#050507' });

  // 3.5mm AUX Out (circular jack)
  mesh(new T.CylinderGeometry(2.4, 2.4, 1.5, 24), jackMat, -11, portPanelY - 6, spineZ - spineD / 2 - 1.2).rotation.x = Math.PI / 2;
  mesh(new T.CylinderGeometry(1.5, 1.5, 2.0, 24), holeMat, -11, portPanelY - 6, spineZ - spineD / 2 - 1.3).rotation.x = Math.PI / 2;

  // HDMI Out port (trapezoidal / rectangular cutout)
  mesh(new T.BoxGeometry(9.0, 4.2, 2.0), jackMat, 0, portPanelY + 3, spineZ - spineD / 2 - 1.2);
  mesh(new T.BoxGeometry(7.2, 2.8, 2.5), holeMat, 0, portPanelY + 3, spineZ - spineD / 2 - 1.3);

  // DC Power IN port (circular barrel jack)
  mesh(new T.CylinderGeometry(3.6, 3.6, 1.5, 24), jackMat, 12, portPanelY - 6, spineZ - spineD / 2 - 1.2).rotation.x = Math.PI / 2;
  mesh(new T.CylinderGeometry(2.2, 2.2, 2.0, 24), holeMat, 12, portPanelY - 6, spineZ - spineD / 2 - 1.3).rotation.x = Math.PI / 2;
  mesh(new T.CylinderGeometry(0.7, 0.7, 2.2, 16), jackMat, 12, portPanelY - 6, spineZ - spineD / 2 - 1.4).rotation.x = Math.PI / 2;

  // =========================================================================
  // 3. ПЕРЕД И КОРПУС (Acoustic Fabric Wrap + Front LED Matrix Display)
  // =========================================================================
  // Main speaker body wrapped in acoustic fabric (front & side faces)
  mesh(roundedBox(w, h, d, r, 3), fabric, 0, y0);

  // Dynamic 25x16 Dot Matrix Display (shining through acoustic fabric weave)
  const cols = 25, rows = 16;
  const canvasW = 512, canvasH = 328;
  const canvas = document.createElement('canvas');
  canvas.width = canvasW;
  canvas.height = canvasH;
  const ctx = canvas.getContext('2d');
  const tex = new T.CanvasTexture(canvas);
  tex.colorSpace = T.SRGBColorSpace;
  tex.anisotropy = 4;

  // Screen placed on front face (Photo 3: centered vertically slightly above midpoint)
  const matrixW = 86, matrixH = 55;
  const matrixY = y0 + h * 0.08;
  const matrixZ = d / 2 + 0.6;

  const displayMesh = mesh(
    new T.PlaneGeometry(matrixW, matrixH),
    new T.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false }),
    0, matrixY, matrixZ
  );
  displayMesh.renderOrder = 3;

  let lastMatrixStr = '';
  function updateDisplay(values) {
    let pixels = null;
    if (Array.isArray(values)) {
      if (values.length >= 400) {
        pixels = values.slice(0, 400);
      } else if (values.length === 36) {
        pixels = new Array(400).fill(0);
        const digitX = [1, 7, 14, 20];
        for (let di = 0; di < 4; di++) {
          const x0 = digitX[di];
          const A = values[di * 8 + 0]; if (A > 0) for (let x = x0 + 1; x < x0 + 4; x++) pixels[3 * cols + x] = A;
          const B = values[di * 8 + 1]; if (B > 0) for (let y = 4; y < 8; y++) pixels[y * cols + x0 + 4] = B;
          const C = values[di * 8 + 2]; if (C > 0) for (let y = 9; y < 13; y++) pixels[y * cols + x0 + 4] = C;
          const D = values[di * 8 + 3]; if (D > 0) for (let x = x0 + 1; x < x0 + 4; x++) pixels[13 * cols + x] = D;
          const E = values[di * 8 + 4]; if (E > 0) for (let y = 9; y < 13; y++) pixels[y * cols + x0 + 4] = E;
          const F = values[di * 8 + 5]; if (F > 0) for (let y = 4; y < 8; y++) pixels[y * cols + x0 + 4] = F;
          const G = values[di * 8 + 6]; if (G > 0) for (let x = x0 + 1; x < x0 + 4; x++) pixels[8 * cols + x] = G;
          const H = values[di * 8 + 7]; if (H > 0 && x0 + 5 < cols) pixels[13 * cols + x0 + 5] = H;
        }
        if (values[32] > 0) { pixels[5 * cols + 12] = values[32]; pixels[6 * cols + 12] = values[32]; }
        if (values[33] > 0) { pixels[10 * cols + 12] = values[33]; pixels[11 * cols + 12] = values[33]; }
        if (values[34] > 0) { for (let x = 11; x < 14; x++) pixels[8 * cols + x] = values[34]; }
        if (values[35] > 0) {
          pixels[3 * cols + 22] = pixels[3 * cols + 23] = values[35];
          pixels[4 * cols + 21] = pixels[4 * cols + 24] = values[35];
          pixels[5 * cols + 22] = pixels[5 * cols + 23] = values[35];
        }
      }
    } else if (values && values.pixels && values.pixels.length >= 400) {
      pixels = values.pixels;
    }

    if (!pixels) return;
    const str = pixels.join(',');
    if (str === lastMatrixStr) return;
    lastMatrixStr = str;

    ctx.clearRect(0, 0, canvasW, canvasH);
    const p = canvasW / cols;
    const ry = (canvasH - p * rows) / 2;
    const iceBlue = '#ebf5ff';

    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < cols; col++) {
        const val = pixels ? (Number(pixels[row * cols + col]) || 0) : 0;
        const alpha = Math.max(0, Math.min(255, val)) / 255;
        const on = alpha > 0.05;

        // Realistic dot through fabric with soft radial glow
        ctx.fillStyle = on ? `rgba(235, 245, 255, ${Math.max(0.72, alpha)})` : 'rgba(255, 255, 255, 0.025)';
        ctx.shadowColor = on ? iceBlue : 'transparent';
        ctx.shadowBlur = on ? p * 0.75 : 0;
        ctx.beginPath();
        ctx.arc((col + 0.5) * p, ry + (row + 0.5) * p, p * 0.33, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    tex.needsUpdate = true;
  }

  // =========================================================================
  // 2. КОЛЬЦО & 5. КОЛЬЦО СВЕРХУ (Top Plate, Well, 24-LED Ring & Volume Wheel)
  // =========================================================================
  // Top plastic cap plate
  mesh(roundedBox(w, capH, d, r, 2), plastic, 0, t + capH / 2);

  // Circular recessed well (depression in top plate around the knob)
  const wellRadius = 51.5;
  const wellDepth = 3.6;
  const wellGeo = new T.RingGeometry(40, wellRadius, 64);
  const wellMat = new T.MeshStandardMaterial({ color: '#1a1b20', roughness: 0.65, metalness: 0.1 });
  const wellMesh = mesh(wellGeo, wellMat, 0, t + capH - 0.2);
  wellMesh.rotation.x = -Math.PI / 2;

  // 24-LED RGB Ring (inside the well recess, under the floating wheel edge)
  const ringRadius = 45.0;
  const ringInnerRadius = 41.5;
  const step = (Math.PI * 2) / 24;
  const ringMaterials = [];

  for (let i = 0; i < 24; i++) {
    const mat = new T.MeshStandardMaterial({
      color: '#16171d',
      emissive: '#000000',
      emissiveIntensity: 0,
      roughness: 0.35,
      metalness: 0.05
    });
    ringMaterials.push(mat);

    // Vertical cylinder arc for light strip
    mesh(new T.CylinderGeometry(ringRadius, ringRadius, 3.2, 8, 1, true, i * step, step * 0.92), mat, 0, t + capH + 1.2);
    // Flat ring face for top illumination
    const topArc = mesh(new T.RingGeometry(ringInnerRadius, ringRadius, 8, 1, i * step, step * 0.92), mat, 0, t + capH + 2.8);
    topArc.rotation.x = -Math.PI / 2;
  }

  let lastRingStr = '';
  function updateRing(leds) {
    if (!leds || !leds.length) return;
    const str = JSON.stringify(leds);
    if (str === lastRingStr) return;
    lastRingStr = str;
    if (!leds || !leds.length) return;
    for (let i = 0; i < 24; i++) {
      const idx = leds.length === 24 ? i : Math.floor(i * leds.length / 24);
      const [r, g, b] = leds[idx] || [0, 0, 0];
      const rN = Math.max(0, Math.min(255, Number(r) || 0)) / 255;
      const gN = Math.max(0, Math.min(255, Number(g) || 0)) / 255;
      const bN = Math.max(0, Math.min(255, Number(b) || 0)) / 255;
      const lit = (rN > 0 || gN > 0 || bN > 0);
      const color = new T.Color(rN, gN, bN);
      ringMaterials[i].color.copy(lit ? color : new T.Color('#16171d'));
      ringMaterials[i].emissive.copy(color);
      ringMaterials[i].emissiveIntensity = lit ? 1.0 : 0;
    }
  }

  // Floating Volume Wheel / Knob (Photo 2 & Photo 5: disc with angled perimeter slits)
  const knobR = 40.5;
  const knobH = 7.5;
  const knobY = t + capH + knobH / 2 + 1.2;

  // Knob rim texture with angled diagonal ventilation slits (Photo 2 & 5: ~60 angled slots)
  const rimTexture = canvasTexture(1024, (ctx, n) => {
    ctx.fillStyle = '#222329';
    ctx.fillRect(0, 0, n, n);

    ctx.fillStyle = '#0f1014';
    const slotCount = 60;
    const slotW = n / slotCount;
    for (let i = 0; i < slotCount; i++) {
      ctx.save();
      ctx.translate(i * slotW + slotW / 2, n / 2);
      ctx.rotate(-0.55); // Diagonal angled slit tilt per photo
      ctx.fillRect(-slotW * 0.22, -n * 0.38, slotW * 0.44, n * 0.76);
      ctx.restore();
    }
  });
  rimTexture.wrapS = T.RepeatWrapping;
  rimTexture.wrapT = T.ClampToEdgeWrapping;

  const knobRimMat = new T.MeshStandardMaterial({
    map: rimTexture,
    color: '#2a2b32',
    roughness: 0.5,
    metalness: 0.15
  });

  mesh(new T.CylinderGeometry(knobR, knobR + 1.2, knobH, 64), knobRimMat, 0, knobY);

  // Top Face of Volume Knob (Photo 5: Alice Button, Mic Mute Button, Center Mic Pinhole)
  const knobTopTexture = canvasTexture(512, (ctx, n) => {
    ctx.fillStyle = '#24252b';
    ctx.fillRect(0, 0, n, n);

    // Subtle concentric grip grooves
    ctx.strokeStyle = '#1d1e23';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(n / 2, n / 2, n * 0.45, 0, Math.PI * 2);
    ctx.stroke();

    // 1. Left Button: Alice activation (Circle with solid rounded triangle)
    const btnRadius = 42;
    const leftBtnX = n * 0.34;
    const btnY = n / 2;

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.45)';
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    ctx.arc(leftBtnX, btnY, btnRadius, 0, Math.PI * 2);
    ctx.stroke();

    // Alice triangle inside left button
    ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.beginPath();
    const triR = 18;
    ctx.moveTo(leftBtnX, btnY - triR);
    ctx.lineTo(leftBtnX + triR * 0.88, btnY + triR * 0.65);
    ctx.lineTo(leftBtnX - triR * 0.88, btnY + triR * 0.65);
    ctx.closePath();
    ctx.fill();

    // 2. Right Button: Microphone Mute (Circle with mic icon and diagonal slash)
    const rightBtnX = n * 0.66;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.45)';
    ctx.lineWidth = 3.5;
    ctx.beginPath();
    ctx.arc(rightBtnX, btnY, btnRadius, 0, Math.PI * 2);
    ctx.stroke();

    // Microphone symbol with slash
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.lineWidth = 3.2;
    ctx.beginPath();
    // Mic capsule
    ctx.arc(rightBtnX, btnY - 4, 8, Math.PI, 0, false);
    ctx.lineTo(rightBtnX + 8, btnY + 2);
    ctx.arc(rightBtnX, btnY + 2, 8, 0, Math.PI, false);
    ctx.lineTo(rightBtnX - 8, btnY - 4);
    // Mic cradle
    ctx.moveTo(rightBtnX - 12, btnY - 2);
    ctx.arc(rightBtnX, btnY + 2, 12, Math.PI, 0, true);
    ctx.moveTo(rightBtnX, btnY + 14);
    ctx.lineTo(rightBtnX, btnY + 20);
    // Strike-through slash
    ctx.moveTo(rightBtnX - 14, btnY + 15);
    ctx.lineTo(rightBtnX + 14, btnY - 14);
    ctx.stroke();

    // 3. Center microphone pinhole
    ctx.fillStyle = '#0c0d10';
    ctx.beginPath();
    ctx.arc(n / 2, btnY, 4.5, 0, Math.PI * 2);
    ctx.fill();

    // Secondary beamforming mic apertures
    ctx.fillStyle = '#14151a';
    for (let ang = 0; ang < Math.PI * 2; ang += Math.PI / 2) {
      const hx = n / 2 + Math.cos(ang) * (n * 0.41);
      const hy = n / 2 + Math.sin(ang) * (n * 0.41);
      ctx.beginPath();
      ctx.arc(hx, hy, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
  });

  const knobCapMesh = mesh(
    new T.CircleGeometry(knobR, 64),
    new T.MeshStandardMaterial({
      map: knobTopTexture,
      roughness: 0.45,
      metalness: 0.1
    }),
    0, knobY + knobH / 2 + 0.05
  );
  knobCapMesh.rotation.x = -Math.PI / 2;

  // Initialize demo display with clean time "12:50" (as seen on Photo 3)
  const initialV = [
    ...[0,1,1,0,0,0,0,0].map(x=>x*255), // 1
    ...[1,1,0,1,1,0,1,0].map(x=>x*255), // 2
    ...[1,0,1,1,0,1,1,0].map(x=>x*255), // 5
    ...[1,1,1,1,1,1,0,0].map(x=>x*255), // 0
    255, 255, 0, 0
  ];
  updateDisplay(initialV);

  return {
    topY: H / 2,
    baseY: -H / 2,
    t,
    display: updateDisplay,
    ring: updateRing
  };
}


export const CONCEPTS = {
  // 141 × 141 × 231 mm; pixel LED display 25 × 16 behind the fabric, volume ring on top.
  max: { name: 'Яндекс Станция Макс', colors: MAX, build: buildMax },
};
