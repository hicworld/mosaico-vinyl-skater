#include "foot_effects.h"

#include <math.h>
#include <stddef.h>

#define FX_PI 3.14159265358979323846f
#define FX_TAU (2.0f * FX_PI)
#define FX_VIEWPORT 480
#define FX_SAMPLE_RATE 16000u
#define FX_BUCKET 2048u

typedef struct {
    foot_effect_plot_t plot;
    void *user;
} brush_t;

static float clamp01(float value)
{
    /* Match fmin(1,fmax(0,value)), including NaN ->0, without two
     * out-of-line libc calls for every coverage/projection sample on S31. */
    return !(value > 0.0f) ? 0.0f : (value < 1.0f ? value : 1.0f);
}

static uint16_t colour(unsigned r, unsigned g, unsigned b)
{
    return (uint16_t)(((r >> 3u) << 11u) | ((g >> 2u) << 5u) | (b >> 3u));
}

static uint32_t hash32(uint32_t value)
{
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    return value ^ (value >> 16u);
}

static float unit(uint32_t value)
{
    return (float)(hash32(value) & 65535u) / 65535.0f;
}

/* Analytic round-ended antialiased strokes: smooth subpixel motion without
 * frame-seeded noise or pixel snapping. All access is clipped before callback. */
static void line(brush_t b, float ax, float ay, float bx, float by, float width,
                 uint16_t rgb, float alpha)
{
    if (alpha < 0.5f || width <= 0.0f) {
        return;
    }
    float radius = width * 0.5f;
    int left = (int)floorf(fminf(ax, bx) - radius - 0.6f);
    int right = (int)ceilf(fmaxf(ax, bx) + radius + 0.6f);
    int top = (int)floorf(fminf(ay, by) - radius - 0.6f);
    int bottom = (int)ceilf(fmaxf(ay, by) + radius + 0.6f);
    if (left < 0) { left = 0; }
    if (right >= FX_VIEWPORT) { right = FX_VIEWPORT - 1; }
    if (top < 0) { top = 0; }
    if (bottom >= FX_VIEWPORT) { bottom = FX_VIEWPORT - 1; }
    float dx = bx - ax, dy = by - ay;
    float length2 = dx * dx + dy * dy;
    for (int y = top; y <= bottom; ++y) {
        for (int x = left; x <= right; ++x) {
            float px = (float)x + 0.5f - ax;
            float py = (float)y + 0.5f - ay;
            float t = length2 > 0.0001f ? clamp01((px * dx + py * dy) / length2) : 0.0f;
            float ex = px - t * dx, ey = py - t * dy;
            float distance2 = ex * ex + ey * ey;
            /* Outside pixels cannot reach even half an alpha step. The small
             * conservative margin keeps the original floating edge intact. */
            float outer = radius + 0.6001f;
            if (distance2 > outer * outer) { continue; }
            float coverage = clamp01(radius + 0.6f - sqrtf(distance2));
            float opacity = alpha * coverage + 0.5f;
            unsigned a = (unsigned)(opacity < 255.0f ? opacity : 255.0f);
            if (a != 0u) {
                b.plot(b.user, x, y, rgb, a);
            }
        }
    }
}

static void dot(brush_t b, float x, float y, float size, uint16_t rgb, float alpha)
{
    line(b, x, y, x, y, size, rgb, alpha);
}

static void ellipse(brush_t b, float x, float y, float rx, float ry, float angle,
                    float width, uint16_t rgb, float alpha)
{
    const unsigned count = 20u;
    float co = cosf(angle), si = sinf(angle);
    float old_x = x + rx * co, old_y = y + rx * si;
    for (unsigned i = 1u; i <= count; ++i) {
        float a = FX_TAU * (float)i / (float)count;
        float u = rx * cosf(a), v = ry * sinf(a);
        float new_x = x + u * co - v * si;
        float new_y = y + u * si + v * co;
        line(b, old_x, old_y, new_x, new_y, width, rgb, alpha);
        old_x = new_x;
        old_y = new_y;
    }
}

static void star(brush_t b, float x, float y, float radius, float angle,
                 uint16_t rgb, float alpha)
{
    float dx = cosf(angle) * radius, dy = sinf(angle) * radius;
    line(b, x - dx, y - dy, x + dx, y + dy, 0.9f, rgb, alpha);
    line(b, x - dy * 0.65f, y + dx * 0.65f,
         x + dy * 0.65f, y - dx * 0.65f, 0.8f, rgb, alpha * 0.85f);
}

static void diamond(brush_t b, float x, float y, float radius, float angle,
                    uint16_t rgb, float alpha)
{
    float dx = cosf(angle) * radius, dy = sinf(angle) * radius;
    float sx = -sinf(angle) * radius * 0.53f, sy = cosf(angle) * radius * 0.53f;
    line(b, x + dx, y + dy, x + sx, y + sy, 0.9f, rgb, alpha);
    line(b, x + sx, y + sy, x - dx, y - dy, 0.9f, rgb, alpha);
    line(b, x - dx, y - dy, x - sx, y - sy, 0.9f, rgb, alpha);
    line(b, x - sx, y - sy, x + dx, y + dy, 0.9f, rgb, alpha);
}

/* The three lightning layers share exactly the same segment distance.
 * Interleave their writes per pixel: each destination keeps halo, cyan, core
 * in the original order while the projection and square root run only once. */
static void bolt_stroke(brush_t b, float ax, float ay, float bx, float by, uint16_t core, float alpha)
{
    if (alpha < 0.5f) { return; }
    static const float radii[3] = {2.8f * 0.5f, 1.25f * 0.5f, 0.85f * 0.5f};
    const uint16_t colours[3] = {colour(30u, 89u, 255u), colour(77u, 229u, 255u), core};
    const float alphas[3] = {alpha * 0.24f, alpha * 0.83f, alpha};
    int left = (int)floorf(fminf(ax, bx) - radii[0] - 0.6f);
    int right = (int)ceilf(fmaxf(ax, bx) + radii[0] + 0.6f);
    int top = (int)floorf(fminf(ay, by) - radii[0] - 0.6f);
    int bottom = (int)ceilf(fmaxf(ay, by) + radii[0] + 0.6f);
    if (left < 0) { left = 0; }
    if (right >= FX_VIEWPORT) { right = FX_VIEWPORT - 1; }
    if (top < 0) { top = 0; }
    if (bottom >= FX_VIEWPORT) { bottom = FX_VIEWPORT - 1; }
    float dx = bx - ax, dy = by - ay, length2 = dx * dx + dy * dy;
    for (int y = top; y <= bottom; ++y) for (int x = left; x <= right; ++x) {
        float px = (float)x + 0.5f - ax, py = (float)y + 0.5f - ay;
        float t = length2 > 0.0001f ? clamp01((px * dx + py * dy) / length2) : 0.0f;
        float ex = px - t * dx, ey = py - t * dy, distance2 = ex * ex + ey * ey;
        if (distance2 > (radii[0] + 0.6001f) * (radii[0] + 0.6001f)) { continue; }
        float distance = sqrtf(distance2);
        for (unsigned layer = 0; layer < 3; ++layer) {
            if (alphas[layer] < 0.5f) { continue; }
            float coverage = clamp01(radii[layer] + 0.6f - distance);
            float opacity = alphas[layer] * coverage + 0.5f;
            unsigned a = (unsigned)(opacity < 255.0f ? opacity : 255.0f);
            if (a) { b.plot(b.user, x, y, colours[layer], a); }
        }
    }
}

static void bolt(brush_t b, float ax, float ay, float bx, float by, uint32_t seed,
                 float strength, float alpha)
{
    float dx = bx - ax, dy = by - ay;
    float length = sqrtf(dx * dx + dy * dy);
    if (length < 0.1f) {
        return;
    }
    float nx = -dy / length, ny = dx / length;
    float old_x = ax, old_y = ay;
    for (unsigned i = 1u; i <= 5u; ++i) {
        float t = (float)i * 0.2f;
        /* Alternating corners read as a lightning bolt at native 480px scale.
         * The bucket seed sets the shape once, then only its envelope fades. */
        float side = ((i + (seed & 1u)) & 1u) != 0u ? 1.0f : -1.0f;
        float spread = i == 5u ? 0.0f : side * (0.95f + unit(seed + i * 23u) * 0.65f) * strength;
        float x = ax + dx * t + nx * spread;
        float y = ay + dy * t + ny * spread;
        uint16_t core = (i & 1u) != 0u ? colour(255u, 255u, 163u) : colour(230u, 255u, 255u);
        bolt_stroke(b, old_x, old_y, x, y, core, alpha);
        old_x = x;
        old_y = y;
    }
    dot(b, bx, by, 1.4f, colour(244u, 253u, 120u), alpha);
}

unsigned foot_effect_resolve(unsigned mode, unsigned skin)
{
    if (mode >= FOOT_EFFECT_COUNT) {
        mode = FOOT_EFFECT_AUTO;
    }
    if (mode == FOOT_EFFECT_AUTO) {
        static const unsigned character_modes[4] = {
            FOOT_EFFECT_SPARKS, FOOT_EFFECT_CRYSTALS, FOOT_EFFECT_ORBITS, FOOT_EFFECT_LIGHTNING
        };
        return character_modes[skin % 4u];
    }
    return mode;
}

static void direction(const foot_effect_foot_t *foot, float *dx, float *dy, float *speed)
{
    float length = sqrtf(foot->velocity_x * foot->velocity_x + foot->velocity_y * foot->velocity_y);
    *speed = clamp01(length / 90.0f);
    if (length > 2.0f) {
        *dx = -foot->velocity_x / length;
        *dy = -foot->velocity_y / length;
    } else {
        *dx = -0.85f;
        *dy = 0.32f;
    }
}

static void ground(brush_t b, const foot_effect_foot_t *foot, unsigned mode,
                   float energy, float time, unsigned index)
{
    if (!foot->contact) {
        return;
    }
    float load = 0.25f + 0.75f * clamp01(foot->load);
    float alpha = (94.0f + 105.0f * energy) * load;
    float x = foot->ground_x, y = foot->ground_y;
    float phase = time * 11.0f + (float)index * 2.4f;
    if (mode == FOOT_EFFECT_LIGHTNING) {
        float dx, dy, speed;
        direction(foot, &dx, &dy, &speed);
        float reach = 5.0f + 3.0f * energy + 2.0f * speed;
        float a = sinf(phase) * 1.1f;
        ellipse(b, x, y + 0.7f, 4.5f + 1.5f * energy, 1.9f,
                0.0f, 1.7f, colour(37u, 105u, 255u), alpha * 0.23f);
        /* Loaded wheels electrify the surface, separately from the longer
         * speed streaks. These arcs stay attached to actual contact points. */
        bolt(b, x - 1.8f, y + 0.6f, x + dx * reach - dy * 2.5f,
             y + dy * reach * 0.5f + a, index * 37u + 7u,
             0.65f + energy * 0.35f, alpha);
        bolt(b, x + 1.8f, y + 0.5f, x - dx * reach * 0.48f + dy * 2.5f,
             y - dy * reach * 0.3f - a, index * 53u + 19u,
             0.5f, alpha * 0.78f);
    } else if (mode == FOOT_EFFECT_SPARKS) {
        line(b, x - 5.2f, y + 0.5f, x + 5.2f, y + 0.5f,
             3.2f, colour(255u, 128u, 38u), alpha * 0.6f);
    } else if (mode == FOOT_EFFECT_CRYSTALS) {
        ellipse(b, x, y, 6.0f + 2.0f * energy, 2.7f, 0.0f, 1.1f,
                colour(112u, 223u, 255u), alpha * 0.85f);
    } else if (mode == FOOT_EFFECT_ORBITS) {
        ellipse(b, x, y, 8.2f + energy * 2.0f, 3.8f + energy * 0.5f, 0.0f,
                1.25f, colour(208u, 144u, 255u), alpha);
        float a = time * 7.0f + (float)index * 2.4f;
        dot(b, x + cosf(a) * (8.2f + energy * 2.0f), y + sinf(a) * 3.8f,
            2.4f, colour(255u, 232u, 187u), alpha + 35.0f);
    } else if (mode == FOOT_EFFECT_FLAME) {
        ellipse(b, x, y, 6.1f, 2.7f, 0.0f, 2.5f,
                colour(46u, 206u, 212u), alpha * 0.5f);
    }
}

static void moving_shoe(brush_t b, const foot_effect_context_t *context,
                        const foot_effect_foot_t *foot, unsigned mode,
                        float energy, float time, unsigned index)
{
    float dx, dy, speed;
    direction(foot, &dx, &dy, &speed);
    float x = foot->x, y = foot->y;
    float airborne = foot->contact ? 1.0f : 0.78f;
    float alpha = (132.0f + energy * 123.0f) * airborne;
    float phase = time * 13.0f + (float)index * 2.1f;
    uint64_t now_bucket = context->samples / FX_BUCKET;
    if (mode == FOOT_EFFECT_FLAME) {
        /* Three bending ribbons travel with the shoe; no screen-space
         * history is retained, so reverse seeking remains exact. */
        for (unsigned j = 0u; j < 3u; ++j) {
            float offset = ((float)j - 1.0f) * 3.0f;
            float wobble = sinf(phase + (float)j * 1.9f);
            float height = 10.0f + energy * 7.0f + (float)j * 2.0f;
            float ax = x + offset, ay = y - 0.7f;
            float bx = ax + dx * (4.0f + speed * 6.0f) + wobble * 2.2f;
            float by = ay - height * 0.53f + dy * speed;
            float cx = ax + dx * (7.0f + speed * 9.0f) - wobble * 1.4f;
            float cy = ay - height + dy * speed * 1.7f;
            line(b, ax, ay, bx, by, 3.8f, colour(51u, 234u, 224u), alpha * 0.78f);
            line(b, bx, by, cx, cy, 2.1f, colour(185u, 117u, 255u), alpha * 0.85f);
            line(b, ax, ay, bx, by, 1.0f, colour(218u, 255u, 239u), alpha * 0.9f);
        }
        return;
    }
    if (mode == FOOT_EFFECT_ORBITS) {
        float radius = 8.0f + 2.2f * energy;
        float tilt = 0.1f * sinf(time * 9.0f + (float)index * 2.1f);
        ellipse(b, x, y - 2.2f, radius, 3.4f, tilt, 1.2f,
                colour(202u, 143u, 255u), alpha * 0.85f);
        ellipse(b, x, y - 2.2f, radius * 0.76f, 4.5f, -tilt,
                0.85f, colour(107u, 216u, 255u), alpha * 0.65f);
        float a = time * 12.0f + (float)index * 2.1f;
        star(b, x + cosf(a) * radius, y - 2.2f + sinf(a) * 3.4f,
             2.7f + energy * 0.8f, 0.1f, colour(255u, 239u, 199u), alpha);
        return;
    }
    /* A fixed sample bucket creates an event once; overlapping envelopes fade
     * it in/out continuously. Geometry never uses the render frame number. */
    for (unsigned previous = 0u; previous < 3u; ++previous) {
        if (mode == FOOT_EFFECT_LIGHTNING && previous >= 2u) {
            break;
        }
        if (now_bucket < previous) {
            continue;
        }
        uint64_t bucket = now_bucket - previous;
        float age = (float)(context->samples - bucket * FX_BUCKET) / (float)FX_SAMPLE_RATE;
        float life = mode == FOOT_EFFECT_LIGHTNING ? 0.27f : 0.36f;
        if (age >= life) {
            continue;
        }
        float u = age / life;
        float envelope = sinf(FX_PI * u);
        envelope *= envelope;
        uint32_t seed = hash32((uint32_t)bucket ^ ((uint32_t)(bucket >> 32u) * 97u)
                               ^ (index + 1u) * 3413u ^ mode * 313u);
        if (mode == FOOT_EFFECT_LIGHTNING) {
            /* Compact shoe-scale arcs match the footprint of the other
             * effect styles. A sparse fork preserves the lightning outline
             * while leaving the boots and leg articulation visible. */
            float side = unit(seed) > 0.5f ? 1.0f : -1.0f;
            float travel = 8.0f + 7.0f * speed + 4.0f * energy;
            float spread = side * (2.4f + 3.2f * unit(seed + 1u));
            float ax = x - dy * side * 1.5f;
            float ay = y - 0.7f + dx * side;
            float bx = x + dx * travel - dy * spread;
            float by = y + dy * travel * 0.78f + dx * spread * 0.7f - 2.0f;
            bolt(b, ax, ay, bx, by, seed + 5u, 0.8f + 0.2f * energy,
                 alpha * envelope);
            if ((seed & 1u) != 0u) {
                float fork_x = x + dx * travel * 0.58f + dy * side * 4.5f;
                float fork_y = y + dy * travel * 0.45f - dx * side * 3.4f - 2.0f;
                bolt(b, ax + (bx - ax) * 0.37f, ay + (by - ay) * 0.37f,
                     fork_x, fork_y, seed + 13u, 0.6f, alpha * envelope * 0.75f);
            }
            if (context->drum_pulse > 0.35f) {
                star(b, x - dy * side * 5.0f, y + dx * side * 3.0f - 2.0f,
                     1.6f + context->drum_pulse * 0.9f, -0.4f,
                     colour(255u, 254u, 161u), alpha * envelope * context->drum_pulse);
            }
        } else if (mode == FOOT_EFFECT_SPARKS) {
            /* Warm tapered metal sparks point opposite current travel. */
            for (unsigned j = 0u; j < 3u; ++j) {
                float side = (unit(seed + j * 19u) - 0.5f) * 13.0f;
                float travel = u * (11.0f + speed * 16.0f);
                float px = x + dx * travel - dy * side * u;
                float py = y + dy * travel + dx * side * u - sinf(FX_PI * u) * 5.8f;
                float length = 2.7f + energy * 3.3f;
                line(b, px, py, px - dx * length, py - dy * length,
                     1.65f, colour(255u, 161u, 47u), alpha * envelope);
                dot(b, px - dx * length, py - dy * length, 1.9f,
                    colour(255u, 246u, 177u), alpha * envelope);
            }
        } else if (mode == FOOT_EFFECT_CRYSTALS) {
            float side = unit(seed) > 0.5f ? 1.0f : -1.0f;
            float travel = u * (7.0f + speed * 9.0f);
            float px = x + side * (4.0f + 7.0f * u) + dx * travel;
            float py = y - 1.5f - sinf(FX_PI * u) * (7.5f + energy * 4.0f) + dy * travel;
            diamond(b, px, py, 2.6f + energy * 1.25f,
                    -FX_PI * 0.5f + 0.28f * sinf(phase + (float)previous),
                    colour(168u, 245u, 255u), alpha * envelope);
            star(b, px - side * 4.0f, py + 3.0f, 1.6f + energy,
                 time * 4.0f + (float)index * 1.6f,
                 colour(125u, 212u, 255u), alpha * envelope * 0.8f);
            dot(b, px, py, 1.3f, colour(251u, 255u, 255u), alpha * envelope);
        }
    }
}

void foot_effect_draw(const foot_effect_context_t *context, unsigned mode, unsigned layer,
                      foot_effect_plot_t plot, void *user)
{
    if (context == NULL || plot == NULL || mode < FOOT_EFFECT_LIGHTNING || mode >= FOOT_EFFECT_COUNT) {
        return;
    }
    brush_t b = {plot, user};
    float energy = clamp01(context->rms * 0.66f + context->drum_pulse * 0.47f);
    /* All continuous oscillators use integer multiples of one 20-second phase.
     * Its wrap is therefore visually continuous, even after hours of playback. */
    float time = FX_TAU * (float)(context->samples % (FX_SAMPLE_RATE * 20u))
                 / (float)(FX_SAMPLE_RATE * 20u);
    for (unsigned index = 0u; index < 2u; ++index) {
        const foot_effect_foot_t *foot = &context->feet[index];
        if (!isfinite(foot->x) || !isfinite(foot->y)
            || !isfinite(foot->ground_x) || !isfinite(foot->ground_y)) {
            continue;
        }
        if (layer == 0u) {
            ground(b, foot, mode, energy, time, index);
        } else if (layer == 1u) {
            moving_shoe(b, context, foot, mode, energy, time, index);
        }
    }
}

void foot_effect_draw_landing(const foot_effect_context_t *context, unsigned mode,
                              float x, float y, float age, float strength, uint32_t seed,
                              foot_effect_plot_t plot, void *user)
{
    if (context == NULL || plot == NULL || mode < FOOT_EFFECT_LIGHTNING || mode >= FOOT_EFFECT_COUNT
        || !isfinite(x) || !isfinite(y) || age < 0.0f || age > 0.46f) {
        return;
    }
    brush_t b = {plot, user};
    float u = age / 0.46f;
    float fade = (1.0f - u) * (1.0f - 0.55f * u);
    float expand = sqrtf(u);
    float energy = clamp01(strength * 0.62f + context->rms * 0.24f + context->drum_pulse * 0.2f);
    float alpha = (174.0f + energy * 81.0f) * fade;
    if (mode == FOOT_EFFECT_LIGHTNING) {
        /* Keep touchdown readable at the same scale as sparks and star rings,
         * with a compact flash and fewer cracks instead of a large light disk. */
        float radius = 4.0f + (19.0f + 7.0f * energy) * expand;
        ellipse(b, x, y + 0.5f, radius, radius * 0.36f, 0.0f, 2.5f,
                colour(29u, 92u, 255u), alpha * 0.31f);
        ellipse(b, x, y + 0.5f, radius, radius * 0.36f, 0.0f, 1.0f,
                colour(120u, 242u, 255u), alpha * 0.9f);
        float flash = clamp01(1.0f - age / 0.085f);
        ellipse(b, x, y, 2.5f + 4.0f * (1.0f - flash), 1.8f + 2.6f * (1.0f - flash),
                0.0f, 2.0f, colour(239u, 255u, 180u), 220.0f * flash);
        star(b, x, y - 1.0f, 3.5f + 2.0f * energy, 0.0f,
             colour(248u, 255u, 225u), 220.0f * flash);
        for (unsigned i = 0u; i < 6u; ++i) {
            float a = (float)i * FX_TAU / 6.0f + (unit(seed + i * 31u) - 0.5f) * 0.35f;
            float reach = 4.0f + expand * (15.0f + 8.0f * energy)
                          * (0.8f + 0.2f * unit(seed + i * 7u));
            float px = x + cosf(a) * reach;
            float py = y + sinf(a) * reach * 0.44f;
            float ax = x + cosf(a) * 2.0f;
            float ay = y + sinf(a) * 0.9f;
            bolt(b, ax, ay, px, py, seed + i * 53u, 0.8f + energy * 0.2f, alpha * 0.8f);
            if (i % 3u == 0u) {
                float branch = a + (((i & 2u) != 0u) ? 0.85f : -0.85f);
                bolt(b, x + (px - x) * 0.52f, y + (py - y) * 0.52f,
                     px + cosf(branch) * (3.0f + energy * 3.0f),
                     py + sinf(branch) * 3.0f - 1.0f,
                     seed + i * 71u + 3u, 0.5f, alpha * 0.65f);
            }
            float rise = sinf(FX_PI * u) * (3.0f + 4.0f * unit(seed + i));
            dot(b, px, py - rise, 1.5f,
                colour(248u, 255u, 133u), alpha * 0.88f);
        }
        return;
    }
    if (mode == FOOT_EFFECT_ORBITS) {
        float radius = 6.0f + 28.0f * expand;
        ellipse(b, x, y, radius, radius * 0.42f, 0.0f, 1.65f,
                colour(217u, 162u, 255u), alpha);
        ellipse(b, x, y, radius * 0.68f, radius * 0.28f, 0.0f, 1.15f,
                colour(255u, 223u, 175u), alpha * 0.85f);
        for (unsigned i = 0u; i < 6u; ++i) {
            float a = (float)i * FX_TAU / 6.0f + 0.5f * u;
            star(b, x + cosf(a) * radius, y + sinf(a) * radius * 0.42f,
                 2.0f + 1.2f * energy, a,
                 colour(255u, 242u, 204u), alpha);
        }
        return;
    }
    uint16_t ring_colour = mode == FOOT_EFFECT_SPARKS ? colour(255u, 151u, 46u)
                          : mode == FOOT_EFFECT_CRYSTALS ? colour(130u, 237u, 255u)
                          : colour(72u, 235u, 228u);
    float radius = 4.0f + expand * (20.0f + 8.0f * energy);
    ellipse(b, x, y, radius, radius * 0.39f, 0.0f, 1.2f,
            ring_colour, alpha * 0.65f);
    for (unsigned i = 0u; i < 8u; ++i) {
        float a = (float)i * FX_TAU / 8.0f + (unit(seed + i * 31u) - 0.5f) * 0.42f;
        float distance = 4.0f + expand * (16.0f + energy * 13.0f);
        float px = x + cosf(a) * distance;
        float py = y + sinf(a) * distance * 0.46f;
        if (mode == FOOT_EFFECT_SPARKS) {
            float rise = sinf(FX_PI * u) * (6.0f + unit(seed + i) * 7.0f);
            line(b, px, py - rise, px - cosf(a) * 4.6f, py - rise - sinf(a) * 2.1f,
                 1.5f, colour(255u, 186u, 60u), alpha);
            dot(b, px, py - rise, 2.2f, colour(255u, 245u, 190u), alpha);
        } else if (mode == FOOT_EFFECT_CRYSTALS) {
            diamond(b, px, py - sinf(FX_PI * u) * 9.0f, 2.5f + energy * 1.0f,
                    a + FX_PI * 0.5f, colour(173u, 241u, 255u), alpha);
            star(b, px + cosf(a) * 3.0f, py - sinf(FX_PI * u) * 4.0f,
                 2.1f, a, colour(237u, 255u, 255u), alpha * 0.8f);
        } else if (mode == FOOT_EFFECT_FLAME) {
            float rise = sinf(FX_PI * u) * (9.0f + energy * 6.0f);
            line(b, px, py, px + cosf(a + 0.8f) * 3.4f, py - rise,
                 3.1f, colour(48u, 223u, 213u), alpha * 0.88f);
            dot(b, px + cosf(a + 0.8f) * 3.4f, py - rise, 2.2f,
                colour(194u, 130u, 255u), alpha);
        }
    }
}
