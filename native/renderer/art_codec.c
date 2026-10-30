#include "art_codec.h"
#include <string.h>
bool art_decode(const uint8_t *src,size_t packed,uint8_t *dst,size_t raw) {
    size_t ip=0,op=0;
    while(ip<packed) {
        unsigned token=src[ip++];size_t n=token>>4;
        if(n==15) {unsigned b;do {if(ip>=packed)return false;b=src[ip++];n+=b;}while(b==255);}
        if(n>packed-ip||n>raw-op)return false;
        memcpy(dst+op,src+ip,n);ip+=n;op+=n;
        if(ip==packed)return op==raw;
        if(packed-ip<2)return false;
        unsigned distance=src[ip]|((unsigned)src[ip+1]<<8);ip+=2;
        if(!distance||distance>op)return false;
        n=(token&15)+4;
        if((token&15)==15) {unsigned b;do {if(ip>=packed)return false;b=src[ip++];n+=b;}while(b==255);}
        if(n>raw-op)return false;
        /* Seed one period, then expand the completed prefix. Every memcpy
         * remains nonoverlapping, including distance-1/2 flat-colour runs,
         * while the number of copies grows logarithmically with match size. */
        size_t start=op,available=distance;
        size_t chunk=n<available?n:available;
        memcpy(dst+op,dst+op-distance,chunk);op+=chunk;n-=chunk;
        while(n) {
            chunk=n<available?n:available;
            memcpy(dst+op,dst+start,chunk);op+=chunk;n-=chunk;available+=chunk;
        }
    }
    return op==raw;
}
bool art_overlay(const uint16_t *runs,size_t words,uint16_t *pixels,size_t count) {
    size_t pos=0;
    for(size_t i=0;i+1<words;i+=2) {
        unsigned encoded=runs[i],n=encoded&0x7fff;
        if(!n||n>count-pos)return false;
        if(!(encoded&0x8000))for(unsigned j=0;j<n;j++)pixels[pos+j]=runs[i+1];
        pos+=n;
    }
    return !(words&1)&&pos==count;
}
bool art_overlay_indexed(const uint8_t *indices,size_t bytes,const uint16_t *palette,size_t palette_count,uint16_t *pixels,size_t count) {
    if(bytes!=count||!palette_count||palette_count>255||!palette
       ||(count&&(!indices||!pixels)))return false;
    /* Validate the whole tile before touching its decoded background. */
    for(size_t i=0;i<count;i++) {
        unsigned index=indices[i];
        if(index!=255&&index>=palette_count)return false;
    }
    for(size_t i=0;i<count;i++) {
        unsigned index=indices[i];
        if(index!=255)pixels[i]=palette[index];
    }
    return true;
}
