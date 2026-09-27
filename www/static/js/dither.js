/* Vanilla WebGL implementation of the React Bits "dither" effect */

const DITHER_CONFIG = {
    waveSpeed:              0.01,
    waveFrequency:          4.0,
    waveAmplitude:          0.25,
    waveColor:              [0.5, 0.5, 0.5],
    colorNum:               4.0,
    /* If mobile, set pixelSize to 6.0, else set it to 2.0 */
    pixelSize:              document.body.classList.contains('mobile') ? 6.0 : 2.0,
    enableMouseInteraction: false,
    mouseRadius:            1.0,
    disableAnimation:       false,
};

/* Shaders (same as in the React Bits implementation) */
const WAVE_VERT = `
attribute vec2 a_position;
varying vec2 vUv;
void main() {
    vUv = a_position * 0.5 + 0.5;
    gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

const WAVE_FRAG = `
precision highp float;
uniform vec2  resolution;
uniform float time;
uniform float waveSpeed;
uniform float waveFrequency;
uniform float waveAmplitude;
uniform vec3  waveColor;
uniform vec2  mousePos;
uniform int   enableMouseInteraction;
uniform float mouseRadius;

vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
vec2 fade(vec2 t){return t*t*t*(t*(t*6.0-15.0)+10.0);}

float cnoise(vec2 P){
    vec4 Pi=floor(P.xyxy)+vec4(0.0,0.0,1.0,1.0);
    vec4 Pf=fract(P.xyxy)-vec4(0.0,0.0,1.0,1.0);
    Pi=mod289(Pi);
    vec4 ix=Pi.xzxz,iy=Pi.yyww,fx=Pf.xzxz,fy=Pf.yyww;
    vec4 i=permute(permute(ix)+iy);
    vec4 gx=fract(i*(1.0/41.0))*2.0-1.0;
    vec4 gy=abs(gx)-0.5;
    vec4 tx=floor(gx+0.5);
    gx=gx-tx;
    vec2 g00=vec2(gx.x,gy.x),g10=vec2(gx.y,gy.y),g01=vec2(gx.z,gy.z),g11=vec2(gx.w,gy.w);
    vec4 norm=taylorInvSqrt(vec4(dot(g00,g00),dot(g01,g01),dot(g10,g10),dot(g11,g11)));
    g00*=norm.x;g01*=norm.y;g10*=norm.z;g11*=norm.w;
    float n00=dot(g00,vec2(fx.x,fy.x));
    float n10=dot(g10,vec2(fx.y,fy.y));
    float n01=dot(g01,vec2(fx.z,fy.z));
    float n11=dot(g11,vec2(fx.w,fy.w));
    vec2 fade_xy=fade(Pf.xy);
    vec2 n_x=mix(vec2(n00,n01),vec2(n10,n11),fade_xy.x);
    return 2.3*mix(n_x.x,n_x.y,fade_xy.y);
}

const int OCTAVES=4;
float fbm(vec2 p){
    float value=0.0,amp=1.0,freq=waveFrequency;
    for(int i=0;i<OCTAVES;i++){
        value+=amp*abs(cnoise(p));
        p*=freq;
        amp*=waveAmplitude;
    }
    return value;
}

float pattern(vec2 p){
    vec2 p2=p-time*waveSpeed;
    return fbm(p+fbm(p2));
}

void main(){
    vec2 uv=gl_FragCoord.xy/resolution.xy;
    uv-=0.5;
    uv.x*=resolution.x/resolution.y;
    float f=pattern(uv);
    if(enableMouseInteraction==1){
        vec2 mouseNDC=(mousePos/resolution-0.5)*vec2(1.0,-1.0);
        mouseNDC.x*=resolution.x/resolution.y;
        float dist=length(uv-mouseNDC);
        float effect=1.0-smoothstep(0.0,mouseRadius,dist);
        f-=0.5*effect;
    }
    vec3 col=mix(vec3(0.0),waveColor,f);
    gl_FragColor=vec4(col,1.0);
}
`;

const DITHER_VERT = `
attribute vec2 a_position;
varying vec2 vUv;
void main(){
    vUv = a_position * 0.5 + 0.5;
    gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

const DITHER_FRAG = `
precision highp float;
uniform sampler2D inputBuffer;
uniform vec2  resolution;
uniform float colorNum;
uniform float pixelSize;

// Bayer 8x8 as a plain float function — no arrays, no bitwise ops, GLSL ES 1.00 safe.
float bayer8(int x, int y) {
    int i = y * 8 + x;
    if (i ==  0) return  0.0/64.0; if (i ==  1) return 48.0/64.0;
    if (i ==  2) return 12.0/64.0; if (i ==  3) return 60.0/64.0;
    if (i ==  4) return  3.0/64.0; if (i ==  5) return 51.0/64.0;
    if (i ==  6) return 15.0/64.0; if (i ==  7) return 63.0/64.0;
    if (i ==  8) return 32.0/64.0; if (i ==  9) return 16.0/64.0;
    if (i == 10) return 44.0/64.0; if (i == 11) return 28.0/64.0;
    if (i == 12) return 35.0/64.0; if (i == 13) return 19.0/64.0;
    if (i == 14) return 47.0/64.0; if (i == 15) return 31.0/64.0;
    if (i == 16) return  8.0/64.0; if (i == 17) return 56.0/64.0;
    if (i == 18) return  4.0/64.0; if (i == 19) return 52.0/64.0;
    if (i == 20) return 11.0/64.0; if (i == 21) return 59.0/64.0;
    if (i == 22) return  7.0/64.0; if (i == 23) return 55.0/64.0;
    if (i == 24) return 40.0/64.0; if (i == 25) return 24.0/64.0;
    if (i == 26) return 36.0/64.0; if (i == 27) return 20.0/64.0;
    if (i == 28) return 43.0/64.0; if (i == 29) return 27.0/64.0;
    if (i == 30) return 39.0/64.0; if (i == 31) return 23.0/64.0;
    if (i == 32) return  2.0/64.0; if (i == 33) return 50.0/64.0;
    if (i == 34) return 14.0/64.0; if (i == 35) return 62.0/64.0;
    if (i == 36) return  1.0/64.0; if (i == 37) return 49.0/64.0;
    if (i == 38) return 13.0/64.0; if (i == 39) return 61.0/64.0;
    if (i == 40) return 34.0/64.0; if (i == 41) return 18.0/64.0;
    if (i == 42) return 46.0/64.0; if (i == 43) return 30.0/64.0;
    if (i == 44) return 33.0/64.0; if (i == 45) return 17.0/64.0;
    if (i == 46) return 45.0/64.0; if (i == 47) return 29.0/64.0;
    if (i == 48) return 10.0/64.0; if (i == 49) return 58.0/64.0;
    if (i == 50) return  6.0/64.0; if (i == 51) return 54.0/64.0;
    if (i == 52) return  9.0/64.0; if (i == 53) return 57.0/64.0;
    if (i == 54) return  5.0/64.0; if (i == 55) return 53.0/64.0;
    if (i == 56) return 42.0/64.0; if (i == 57) return 26.0/64.0;
    if (i == 58) return 38.0/64.0; if (i == 59) return 22.0/64.0;
    if (i == 60) return 41.0/64.0; if (i == 61) return 25.0/64.0;
    if (i == 62) return 37.0/64.0;
    return 21.0/64.0;
}

vec3 dither(vec2 fragCoord, vec3 color) {
    vec2 scaledCoord = floor(fragCoord / pixelSize);
    int x = int(mod(scaledCoord.x, 8.0));
    int y = int(mod(scaledCoord.y, 8.0));
    float threshold = bayer8(x, y) - 0.25;
    float s = 1.0 / (colorNum - 1.0);
    color += threshold * s;
    color = clamp(color - 0.2, 0.0, 1.0);
    return floor(color * (colorNum - 1.0) + 0.5) / (colorNum - 1.0);
}

void main(){
    vec2 fragCoord = gl_FragCoord.xy;
    vec2 normalizedPixelSize = pixelSize / resolution;
    vec2 uvPixel = normalizedPixelSize * floor((fragCoord / resolution) / normalizedPixelSize);
    vec4 color = texture2D(inputBuffer, uvPixel);
    color.rgb = dither(fragCoord, color.rgb);
    gl_FragColor = color;
}
`;

// WebGL shader stuff

function compileShader(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw new Error('Shader compile error:\n' + gl.getShaderInfoLog(s));
    return s;
}

function createProgram(gl, vert, frag) {
    const p = gl.createProgram();
    gl.attachShader(p, compileShader(gl, gl.VERTEX_SHADER,   vert));
    gl.attachShader(p, compileShader(gl, gl.FRAGMENT_SHADER, frag));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS))
        throw new Error('Program link error:\n' + gl.getProgramInfoLog(p));
    return p;
}

function createQuad(gl, program) {
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
        new Float32Array([-1,-1, 1,-1, -1,1, 1,1]),
        gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(program, 'a_position');
    return { buf, loc };
}

function createFramebuffer(gl, w, h) {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return { fb, tex };
}

/* Main loop */
(function init() {
    // Respect prefers-reduced-motion
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const canvas = document.createElement('canvas');
    Object.assign(canvas.style, {
        position:  'fixed',
        top:       '0',
        left:      '0',
        width:     '100%',
        height:    '100%',
        zIndex:    '-1',
        pointerEvents: 'none',
        display:   'block',
    });
    document.body.insertBefore(canvas, document.body.firstChild);

    const gl = canvas.getContext('webgl', { antialias: true, preserveDrawingBuffer: true });
    if (!gl) {
        console.warn('dither.js: WebGL not supported, skipping effect.');
        return;
    }

    // Check for float array support in fragment shaders

    const waveProg   = createProgram(gl, WAVE_VERT,   WAVE_FRAG);
    const ditherProg = createProgram(gl, DITHER_VERT, DITHER_FRAG);

    const waveQuad   = createQuad(gl, waveProg);
    const ditherQuad = createQuad(gl, ditherProg);

    // Uniform locations wave
    const wLoc = {
        resolution:             gl.getUniformLocation(waveProg, 'resolution'),
        time:                   gl.getUniformLocation(waveProg, 'time'),
        waveSpeed:              gl.getUniformLocation(waveProg, 'waveSpeed'),
        waveFrequency:          gl.getUniformLocation(waveProg, 'waveFrequency'),
        waveAmplitude:          gl.getUniformLocation(waveProg, 'waveAmplitude'),
        waveColor:              gl.getUniformLocation(waveProg, 'waveColor'),
        mousePos:               gl.getUniformLocation(waveProg, 'mousePos'),
        enableMouseInteraction: gl.getUniformLocation(waveProg, 'enableMouseInteraction'),
        mouseRadius:            gl.getUniformLocation(waveProg, 'mouseRadius'),
    };

    // Uniform locations dither
    const dLoc = {
        inputBuffer: gl.getUniformLocation(ditherProg, 'inputBuffer'),
        resolution:  gl.getUniformLocation(ditherProg, 'resolution'),
        colorNum:    gl.getUniformLocation(ditherProg, 'colorNum'),
        pixelSize:   gl.getUniformLocation(ditherProg, 'pixelSize'),
    };

    let fbo = null;
    let w = 0, h = 0;

    function resize() {
        const dpr = window.devicePixelRatio || 1;
        const nw = Math.floor(canvas.clientWidth  * dpr);
        const nh = Math.floor(canvas.clientHeight * dpr);
        if (nw === w && nh === h) return;
        w = nw; h = nh;
        canvas.width  = w;
        canvas.height = h;
        if (fbo) {
            gl.deleteTexture(fbo.tex);
            gl.deleteFramebuffer(fbo.fb);
        }
        fbo = createFramebuffer(gl, w, h);
    }

    // Handle window resize
    const ro = new ResizeObserver(resize);
    ro.observe(document.documentElement);
    resize();

    // Mouse tracking (in physical pixels)
    const mouse = { x: 0, y: 0 };
    if (DITHER_CONFIG.enableMouseInteraction) {
        window.addEventListener('mousemove', e => {
            const dpr = window.devicePixelRatio || 1;
            mouse.x = e.clientX * dpr;
            mouse.y = e.clientY * dpr;
        }, { passive: true });
    }

    let startTime = null;
    let rafId = null;

    function bindQuad(quad) {
        gl.bindBuffer(gl.ARRAY_BUFFER, quad.buf);
        gl.enableVertexAttribArray(quad.loc);
        gl.vertexAttribPointer(quad.loc, 2, gl.FLOAT, false, 0, 0);
    }

    function frame(ts) {
        rafId = requestAnimationFrame(frame);
        if (!fbo || w === 0 || h === 0) return;

        if (startTime === null) startTime = ts;
        const elapsed = DITHER_CONFIG.disableAnimation || prefersReduced
            ? 0.0
            : (ts - startTime) / 1000.0;

        // Pass 1: wave -> framebuffer
        gl.bindFramebuffer(gl.FRAMEBUFFER, fbo.fb);
        gl.viewport(0, 0, w, h);
        gl.useProgram(waveProg);
        bindQuad(waveQuad);

        gl.uniform2f(wLoc.resolution, w, h);
        gl.uniform1f(wLoc.time,          elapsed);
        gl.uniform1f(wLoc.waveSpeed,     DITHER_CONFIG.waveSpeed);
        gl.uniform1f(wLoc.waveFrequency, DITHER_CONFIG.waveFrequency);
        gl.uniform1f(wLoc.waveAmplitude, DITHER_CONFIG.waveAmplitude);
        gl.uniform3fv(wLoc.waveColor,    DITHER_CONFIG.waveColor);
        gl.uniform2f(wLoc.mousePos,      mouse.x, mouse.y);
        gl.uniform1i(wLoc.enableMouseInteraction,
            DITHER_CONFIG.enableMouseInteraction ? 1 : 0);
        gl.uniform1f(wLoc.mouseRadius,   DITHER_CONFIG.mouseRadius);

        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

        // Pass 2: dither -> screen
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, w, h);
        gl.useProgram(ditherProg);
        bindQuad(ditherQuad);

        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, fbo.tex);
        gl.uniform1i(dLoc.inputBuffer, 0);
        gl.uniform2f(dLoc.resolution,  w, h);
        gl.uniform1f(dLoc.colorNum,    DITHER_CONFIG.colorNum);
        gl.uniform1f(dLoc.pixelSize,   DITHER_CONFIG.pixelSize);

        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    rafId = requestAnimationFrame(frame);
})();