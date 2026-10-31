// TW1MP fan page — minimal interactions

// Nav: blur border + active section
const nav = document.getElementById('nav');
const navLinks = [...document.querySelectorAll('.nav-links a')];
const sections = navLinks.map(a => document.querySelector(a.getAttribute('href')));

addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', scrollY > 30);
  let current = null;
  for (const s of sections) {
    if (s && s.getBoundingClientRect().top < innerHeight * 0.4) current = s.id;
  }
  navLinks.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + current));
}, { passive: true });

// Reveal on scroll
const io = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (e.isIntersecting) {
      const el = e.target;
      const group = el.closest('.reveal-group');
      if (group) {
        [...group.querySelectorAll('.reveal')].forEach((c, i) => {
          setTimeout(() => c.classList.add('on'), i * 70);
        });
      } else {
        el.classList.add('on');
      }
      io.unobserve(el);
    }
  }
}, { threshold: 0.12 });
document.querySelectorAll('.reveal').forEach(el => io.observe(el));

// Copy buttons
document.querySelectorAll('.copy').forEach(btn => {
  btn.addEventListener('click', async () => {
    try {
      const pre = btn.parentElement && btn.parentElement.querySelector('pre');
      const text = btn.dataset.copy || (pre ? pre.textContent : '');
      await navigator.clipboard.writeText(text);
      btn.textContent = 'Copied';
      btn.classList.add('done');
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1600);
    } catch (e) { /* clipboard blocked */ }
  });
});

// Lightbox (nur wenn vorhanden — Unterseiten haben keine)
const lb = document.getElementById('lightbox');
if (lb) {
  const lbImg = lb.querySelector('img');
  document.querySelectorAll('.shot').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      lbImg.src = a.href;
      lb.classList.add('open');
    });
  });
  lb.addEventListener('click', () => lb.classList.remove('open'));
  addEventListener('keydown', e => { if (e.key === 'Escape') lb.classList.remove('open'); });
}

// Legal modals
window.openLegal = id => { document.getElementById(id).classList.add("open"); document.body.style.overflow = "hidden"; };
window.closeLegal = id => { document.getElementById(id).classList.remove("open"); document.body.style.overflow = ""; };
document.querySelectorAll(".lmodal").forEach(m => m.addEventListener("click", e => { if (e.target === m) closeLegal(m.id); }));
addEventListener("keydown", e => { if (e.key === "Escape") document.querySelectorAll(".lmodal.open").forEach(m => closeLegal(m.id)); });

// Robuste Anker-Navigation (Nav + Hero-CTA + Scroll-Hint)
document.querySelectorAll('a[href^="#"]').forEach(a => {
  const id = a.getAttribute("href");
  if (id.length < 2) return;
  a.addEventListener("click", e => {
    const target = document.querySelector(id);
    if (!target) return;
    e.preventDefault();
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    history.replaceState(null, "", id);
  });
});

// ═══════════ v5: Trailer, Embers, Tilt, Count-up, Parallax ═══════════

// Scroll progress bar
const prog = document.getElementById('scrollProgress');
addEventListener('scroll', () => {
  const h = document.documentElement.scrollHeight - innerHeight;
  if (prog && h > 0) prog.style.width = (scrollY / h * 100) + '%';
}, { passive: true });

// Trailer: custom play button
const tFrame = document.getElementById('trailerFrame');
const tVideo = document.getElementById('trailerVideo');
const tPlay = document.getElementById('trailerPlay');
if (tPlay && tVideo) {
  tPlay.addEventListener('click', () => {
    tFrame.classList.add('playing');
    tVideo.controls = true;
    tVideo.play();
  });
  tVideo.addEventListener('ended', () => {
    tFrame.classList.remove('playing');
    tVideo.controls = false;
  });
}

// Ambient video: nur abspielen wenn sichtbar (spart Akku)
const amb = document.getElementById('ambientVideo');
if (amb) {
  new IntersectionObserver(es => {
    es.forEach(e => e.isIntersecting ? amb.play().catch(()=>{}) : amb.pause());
  }, { threshold: 0.05 }).observe(amb);
}

// Count-up Stats
const counters = document.querySelectorAll('[data-count]');
if (counters.length) {
  const cio = new IntersectionObserver(es => {
    es.forEach(e => {
      if (!e.isIntersecting) return;
      const el = e.target, target = parseInt(el.dataset.count);
      const t0 = performance.now(), dur = 1200;
      const tick = now => {
        const p = Math.min((now - t0) / dur, 1);
        el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
      cio.unobserve(el);
    });
  }, { threshold: 0.4 });
  counters.forEach(c => cio.observe(c));
}

// 3D-Tilt auf Loot-/Forge-Karten (nur mit Maus, nicht Touch)
if (matchMedia('(hover: hover)').matches) {
  document.querySelectorAll('.tilt').forEach(card => {
    card.addEventListener('pointermove', e => {
      const r = card.getBoundingClientRect();
      const rx = ((e.clientY - r.top) / r.height - 0.5) * -6;
      const ry = ((e.clientX - r.left) / r.width - 0.5) * 6;
      card.style.transform = `perspective(700px) rotateX(${rx}deg) rotateY(${ry}deg) translateY(-2px)`;
    });
    card.addEventListener('pointerleave', () => { card.style.transform = ''; });
  });
}

// Parallax: Forge-Hintergrund
const forgeBg = document.getElementById('forgeBg');
if (forgeBg) {
  addEventListener('scroll', () => {
    const r = forgeBg.closest('.sec-forge').getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) return;
    const p = (r.top + r.height / 2 - innerHeight / 2) / innerHeight;
    forgeBg.style.transform = `translateY(${p * -46}px)`;
  }, { passive: true });
}

// Glut-Partikel im Arsenal (dezent, pausiert wenn unsichtbar)
const embers = document.getElementById('embers');
if (embers) {
  const ctx = embers.getContext('2d');
  let parts = [], running = false, raf = 0;
  const resize = () => {
    const s = embers.closest('.sec-arsenal');
    embers.width = s.offsetWidth; embers.height = s.offsetHeight;
  };
  resize(); addEventListener('resize', resize);
  const spawn = () => ({
    x: Math.random() * embers.width, y: embers.height + 8,
    vy: 0.25 + Math.random() * 0.55, vx: (Math.random() - 0.5) * 0.28,
    r: 0.7 + Math.random() * 1.7, a: 0.12 + Math.random() * 0.4,
    tw: Math.random() * Math.PI * 2
  });
  for (let i = 0; i < 42; i++) { const p = spawn(); p.y = Math.random() * embers.height; parts.push(p); }
  const step = () => {
    if (!running) return;
    ctx.clearRect(0, 0, embers.width, embers.height);
    for (const p of parts) {
      p.y -= p.vy; p.x += p.vx; p.tw += 0.04;
      if (p.y < -10) Object.assign(p, spawn());
      const flicker = 0.7 + 0.3 * Math.sin(p.tw);
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(224,164,72,${(p.a * flicker).toFixed(3)})`;
      ctx.fill();
    }
    raf = requestAnimationFrame(step);
  };
  new IntersectionObserver(es => {
    es.forEach(e => {
      running = e.isIntersecting;
      if (running) step(); else cancelAnimationFrame(raf);
    });
  }, { threshold: 0.02 }).observe(embers);
}

// Vorher/Nachher-Slider
document.querySelectorAll('.ba').forEach(ba => {
  let dragging = false;
  const set = clientX => {
    const r = ba.getBoundingClientRect();
    const p = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    ba.style.setProperty('--pos', (p * 100).toFixed(1) + '%');
  };
  const down = e => { dragging = true; set((e.touches ? e.touches[0] : e).clientX); e.preventDefault(); };
  const move = e => { if (dragging) set((e.touches ? e.touches[0] : e).clientX); };
  const up = () => { dragging = false; };
  ba.addEventListener('pointerdown', down);
  addEventListener('pointermove', move, { passive: true });
  addEventListener('pointerup', up);
  // Touch-Fallback
  ba.addEventListener('touchstart', down, { passive: false });
  addEventListener('touchmove', move, { passive: true });
  addEventListener('touchend', up);
});

// ═══════════ v6: Scroll-Indikator + Parallax-Engine ═══════════

// Scroll-Indikator rechts (Dots + Labels)
const SI_SECTIONS = [
  ['top', 'Home'], ['trailer', 'Trailer'], ['server', 'Server'],
  ['join', 'Join'], ['guides', 'Guides'], ['arsenal', 'Arsenal'], ['shots', 'Gallery'], ['forge', 'Alchimist']
];
const si = document.getElementById('scrollInd');
if (si) {
  for (const [id, label] of SI_SECTIONS) {
    const b = document.createElement('button');
    b.className = 'si-dot'; b.dataset.target = id;
    b.innerHTML = '<span class="dot"></span><span class="lbl">' + label + '</span>';
    b.addEventListener('click', () => {
      document.getElementById(id).scrollIntoView({ behavior: 'smooth' });
      history.replaceState(null, '', id === 'top' ? location.pathname : '#' + id);
    });
    si.appendChild(b);
  }
  const dots = [...si.children];
  addEventListener('scroll', () => {
    let current = 'top';
    for (const [id] of SI_SECTIONS) {
      const el = document.getElementById(id);
      if (el && el.getBoundingClientRect().top < innerHeight * 0.45) current = id;
    }
    dots.forEach(d => d.classList.toggle('active', d.dataset.target === current));
  }, { passive: true });
}

// Parallax-Engine: [data-plx] Elemente + Hero-Ebenen
const plxEls = [...document.querySelectorAll('[data-plx]')];
const heroBgImg = document.querySelector('.hero-bg img');
const heroInner = document.querySelector('.hero-inner');
let plxTick = false;
const plxUpdate = () => {
  plxTick = false;
  // Layer-Parallax im Hero: Bild langsamer, Text schneller + Fade
  if (heroBgImg && scrollY < innerHeight * 1.2) {
    heroBgImg.style.translate = '0 ' + (scrollY * 0.22) + 'px';
    if (heroInner) {
      heroInner.style.translate = '0 ' + (scrollY * 0.42) + 'px';
      heroInner.style.opacity = Math.max(0, 1 - scrollY / (innerHeight * 0.75));
    }
  }
  for (const el of plxEls) {
    const r = el.getBoundingClientRect();
    if (r.bottom < -80 || r.top > innerHeight + 80) continue;
    const p = (r.top + r.height / 2 - innerHeight / 2);
    el.style.translate = '0 ' + (p * parseFloat(el.dataset.plx)).toFixed(1) + 'px';
  }
};
addEventListener('scroll', () => {
  if (!plxTick) { plxTick = true; requestAnimationFrame(plxUpdate); }
}, { passive: true });
plxUpdate();
