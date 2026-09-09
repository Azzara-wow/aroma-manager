/**
 * ============================================================
 *  FOG EFFECT — движущийся туман на three.js
 * ============================================================
 *  Как использовать в новом сайте:
 *
 *  1. Подключить three.js (r128) и этот файл после него:
 *     <canvas id="fog-canvas"></canvas>
 *     <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
 *     <script src="fog.js"></script>
 *
 *  2. Вызвать:
 *     initFog('fog-canvas', {
 *       colors: [...],   // см. FOG_PRESETS ниже, можно взять готовый пресет
 *       speed: 0.00014,
 *       opacity: 0.58,
 *       mouseInfluence: 0.05
 *     });
 *
 *  Больше НИЧЕГО в этом файле трогать не нужно — вся кастомизация
 *  через объект config, который вы передаёте в initFog().
 * ============================================================
 */

/* ------------------------------------------------------------
 * ГОТОВЫЕ ПАЛИТРЫ (пресеты)
 * Каждая — 5 цветов от самого светлого к самому тёмному.
 * Цвет задаётся в формате { r, g, b } от 0 до 1
 * (это НЕ обычный 0-255, поэтому ниже есть функция-конвертер hexToRgb01).
 * ------------------------------------------------------------ */
const FOG_PRESETS = {
  // Between — пудрово-розовый / бордо (оригинал)
  between: ['#EDE8E3', '#DBCEC9', '#B5A9A0', '#D4C5BA', '#6B4F4F'],

  // Пример: тёплая терракота — для керамики / handmade из глины
  terracotta: ['#F3E9DE', '#E6C9B4', '#CE9A75', '#B96A45', '#5C3A2E'],

  // Пример: шалфейно-зелёный — для свечей / трав / эко-мастеров
  sage: ['#EEF0E8', '#D8DFC9', '#AEBB94', '#8FA377', '#3E4A34'],

  // Пример: холодный графит — для минималистичных/лаконичных брендов
  graphite: ['#EDEDED', '#D2D2D2', '#A6A6A6', '#7A7A7A', '#2B2B2B'],
};

/* Конвертер "#RRGGBB" -> {r,g,b} от 0 до 1, чтобы можно было
   задавать цвета обычным hex-кодом, а не вручную считать доли */
function hexToRgb01(hex) {
  hex = hex.replace('#', '');
  return {
    r: parseInt(hex.substring(0, 2), 16) / 255,
    g: parseInt(hex.substring(2, 4), 16) / 255,
    b: parseInt(hex.substring(4, 6), 16) / 255,
  };
}

/* ------------------------------------------------------------
 * ГЛАВНАЯ ФУНКЦИЯ
 * ------------------------------------------------------------ */
function initFog(canvasId, userConfig = {}) {

  // ---------- НАСТРОЙКИ ПО УМОЛЧАНИЮ ----------
  const defaults = {
    colors: FOG_PRESETS.between, // массив из 5 hex-цветов, либо FOG_PRESETS.terracotta и т.п.
    speed: 0.00014,              // скорость движения тумана. 0.00008 — медленно/спокойно, 0.0003 — быстро/тревожно
    opacity: 0.58,                // общая непрозрачность тумана, 0..1
    mouseInfluence: 0.05,         // насколько туман "тянется" за курсором. 0 — не реагирует, 0.15 — сильно реагирует
  };

  const config = { ...defaults, ...userConfig };
  const colorsRgb = config.colors.map(hexToRgb01);

  const canvas = document.getElementById(canvasId);
  if (!canvas) {
    console.error(`[fog.js] canvas с id="${canvasId}" не найден`);
    return;
  }

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);

  const vertexShader = `
    varying vec2 vUv;
    void main(){ vUv = uv; gl_Position = vec4(position, 1.0); }
  `;

  const fragmentShader = `
    precision mediump float;
    varying vec2 vUv;
    uniform float uTime;
    uniform vec2 uResolution;
    uniform vec2 uMouse;
    uniform float uOpacity;
    uniform vec3 uColor0, uColor1, uColor2, uColor3, uColor4;

    vec3 mod289(vec3 x){return x-floor(x*(1./289.))*289.;}
    vec2 mod289(vec2 x){return x-floor(x*(1./289.))*289.;}
    vec3 permute(vec3 x){return mod289(((x*34.)+1.)*x);}
    float snoise(vec2 v){
      const vec4 C=vec4(.211324865405187,.366025403784439,-.577350269189626,.024390243902439);
      vec2 i=floor(v+dot(v,C.yy));
      vec2 x0=v-i+dot(i,C.xx);
      vec2 i1=(x0.x>x0.y)?vec2(1.,0.):vec2(0.,1.);
      vec4 x12=x0.xyxy+C.xxzz; x12.xy-=i1;
      i=mod289(i);
      vec3 p=permute(permute(i.y+vec3(0.,i1.y,1.))+i.x+vec3(0.,i1.x,1.));
      vec3 m=max(.5-vec3(dot(x0,x0),dot(x12.xy,x12.xy),dot(x12.zw,x12.zw)),0.);
      m*=m; m*=m;
      vec3 x2=2.*fract(p*C.www)-1.;
      vec3 h=abs(x2)-.5;
      vec3 ox=floor(x2+.5);
      vec3 a0=x2-ox;
      m*=1.79284291400159-.85373472095314*(a0*a0+h*h);
      vec3 g;
      g.x=a0.x*x0.x+h.x*x0.y;
      g.yz=a0.yz*x12.xz+h.yz*x12.yw;
      return 130.*dot(m,g);
    }

    void main(){
      vec2 uv = vUv;
      uv.x *= uResolution.x / uResolution.y;
      vec2 mo = (uMouse - 0.5) * 0.20;

      float n1 = snoise(vec2(uv.x*0.9 + uTime*0.75, uv.y*0.7  + uTime*0.42) + mo*2.2);
      float n2 = snoise(vec2(uv.x*2.1 - uTime*0.58, uv.y*1.85 + uTime*0.38) + mo*1.2);
      float n3 = snoise(vec2(uv.x*3.8 + uTime*0.95, uv.y*3.3  - uTime*0.65) + mo*0.5);
      float n4 = snoise(vec2(uv.x*6.5 - uTime*1.20, uv.y*5.8  + uTime*0.90));

      float val = n1*0.50 + n2*0.28 + n3*0.14 + n4*0.08;
      val = val*0.5 + 0.5;
      val = pow(val, 0.85);

      vec3 col;
      if      (val < 0.20) col = mix(uColor0, uColor1, val/0.20);
      else if (val < 0.40) col = mix(uColor1, uColor2, (val-0.20)/0.20);
      else if (val < 0.60) col = mix(uColor2, uColor3, (val-0.40)/0.20);
      else if (val < 0.80) col = mix(uColor3, uColor4, (val-0.60)/0.20);
      else                 col = mix(uColor4, uColor0, (val-0.80)/0.20);

      vec2 vig = vUv * (1.0 - vUv.yx);
      float vigVal = pow(vig.x * vig.y * 16.0, 0.25);
      col = mix(col * 0.88, col, vigVal);

      float alpha = uOpacity * (0.60 + val*0.40);
      gl_FragColor = vec4(col, alpha);
    }
  `;

  const uniforms = {
    uTime:       { value: 0.0 },
    uResolution: { value: new THREE.Vector2(window.innerWidth, window.innerHeight) },
    uMouse:      { value: new THREE.Vector2(0.5, 0.5) },
    uOpacity:    { value: config.opacity },
    uColor0: { value: new THREE.Vector3(colorsRgb[0].r, colorsRgb[0].g, colorsRgb[0].b) },
    uColor1: { value: new THREE.Vector3(colorsRgb[1].r, colorsRgb[1].g, colorsRgb[1].b) },
    uColor2: { value: new THREE.Vector3(colorsRgb[2].r, colorsRgb[2].g, colorsRgb[2].b) },
    uColor3: { value: new THREE.Vector3(colorsRgb[3].r, colorsRgb[3].g, colorsRgb[3].b) },
    uColor4: { value: new THREE.Vector3(colorsRgb[4].r, colorsRgb[4].g, colorsRgb[4].b) },
  };

  const material = new THREE.ShaderMaterial({
    vertexShader, fragmentShader, uniforms, transparent: true, depthWrite: false
  });
  scene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), material));

  let tX = 0.5, tY = 0.5, cX = 0.5, cY = 0.5;
  window.addEventListener('mousemove', (e) => {
    tX = e.clientX / window.innerWidth;
    tY = 1 - e.clientY / window.innerHeight;
  }, { passive: true });

  window.addEventListener('resize', () => {
    renderer.setSize(window.innerWidth, window.innerHeight);
    uniforms.uResolution.value.set(window.innerWidth, window.innerHeight);
  });

  const t0 = Date.now();
  function animate() {
    requestAnimationFrame(animate);
    uniforms.uTime.value = (Date.now() - t0) * config.speed;
    cX += (tX - cX) * config.mouseInfluence;
    cY += (tY - cY) * config.mouseInfluence;
    uniforms.uMouse.value.set(cX, cY);
    renderer.render(scene, camera);
  }
  animate();
}
