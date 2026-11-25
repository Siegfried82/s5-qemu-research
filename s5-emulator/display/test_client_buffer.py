#!/usr/bin/env python3
"""Synthetic tests of captured EGL image-pbuffer use; no invented camera UI."""
import struct
import tempfile
import unittest
from graphics_hle import GraphicsHLE
from openvg_backend import UnsupportedVG
from test_graphics_hle import Memory


class ClientBufferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='s5-client-buffer-test-')
        self.emu=Memory();self.driver=GraphicsHLE(self.emu,self.tmp.name);self.b=self.driver.backend
        self.driver.call('eglInitialize',[self.driver.display,0,0])
        self.context=self.driver.call('eglCreateContext',[self.driver.display,self.driver.config,0,0])
        self.window=self.b.create_surface(16,12,'synthetic window')
        self.current(self.window)
    def tearDown(self):self.driver.close();self.tmp.cleanup()
    def current(self,surface):
        self.driver.call('eglMakeCurrent',[self.driver.display,surface,surface,self.context])
    def image(self,raw=None):
        raw=bytes(16*12*4) if raw is None else raw
        raw+=bytes(16*4*4)  # Original DMP allocation pads height12 to16.
        self.emu.write_mem(0x1000,raw)
        return self.driver.call('vgCreateImageStandardDataDMP',[2,16,12,len(raw),0x1000,1])
    def bind(self,image):
        return self.driver.call('eglCreatePbufferFromClientBuffer',[self.driver.display,0x3096,image,self.driver.config,0])

    def test_five_argument_abi_null_attrs_and_derived_geometry(self):
        image=self.image();self.emu.write_mem(0x100,struct.pack('<I',0))
        regs={'r%d'%i:0 for i in range(16)}
        regs.update(r0=self.driver.display,r1=0x3096,r2=image,r3=self.driver.config,
                    r13=0x100,r14=0x7290605,r15=0x64c2878)
        event=self.driver.handle(0x64c2878,regs);surface=event['result']
        self.assertEqual(self.emu.regs[15],0x7290604)
        self.assertEqual((self.b.surfaces[surface].width,self.b.surfaces[surface].height),(16,12))
        self.assertEqual(self.b.surface,self.window)
        self.assertEqual(self.driver.surface_attributes[surface],{0x3088:0x308c,0x3087:0x3089})

    def test_drawn_image_pixels_survive_restore_and_surface_destruction(self):
        image=self.image();surface=self.bind(image);self.current(surface)
        self.b.state[0x1121]=[1,0,0,1];self.b.clear(2,3,6,4)
        self.assertEqual(self.b.image_pixels(image).getpixel((4,7)),(255,0,0,255))
        self.assertEqual(self.b.surfaces[self.window].image().getpixel((4,7)),(0,0,0,0))
        self.current(self.window)
        self.driver.call('eglDestroySurface',[self.driver.display,surface])
        self.assertIn(image,self.b.images);self.assertNotIn(image,self.b.image_surfaces)
        self.driver.call('vgDrawImage',[image])
        self.assertEqual(self.b.surfaces[self.window].image().getpixel((4,7)),(255,0,0,255))
        self.assertEqual(self.b.surfaces[self.window].image().getpixel((0,0)),(0,0,0,0))
        # A subsequent binding must start with the modified image, not the
        # original all-zero capture.
        again=self.bind(image)
        self.assertEqual(self.b.surfaces[again].image().getpixel((4,7)),(255,0,0,255))

    def test_low_alpha_premultiplied_storage_is_not_roundtripped(self):
        # VG_sRGBA_8888_PRE is a little-endian packed RGBA word: A,B,G,R.
        raw=bytes([63,3,7,31])*(16*12);image=self.image(raw);surface=self.bind(image)
        expected=bytes([31,7,3,63])*(16*12)
        self.assertEqual(bytes(self.b.surfaces[surface].buffer),expected)
        self.driver.call('eglDestroySurface',[self.driver.display,surface])
        self.assertEqual(self.b.image_pixels(image,True).tobytes(),expected)
        self.assertEqual(bytes(self.b.surfaces[self.bind(image)].buffer),expected)

    def test_deleting_public_image_retains_bound_surface_without_reviving_handle(self):
        image=self.image();surface=self.bind(image);self.current(surface)
        self.driver.call('vgDestroyImage',[image])
        self.assertNotIn(image,self.b.images)
        self.b.state[0x1121]=[0,1,0,1];self.b.clear(0,0,16,12)
        self.assertEqual(self.b.surfaces[surface].image().getpixel((4,4)),(0,255,0,255))
        self.current(self.window);self.driver.call('eglDestroySurface',[self.driver.display,surface])
        self.assertNotIn(image,self.b.images);self.assertNotIn(image,self.b.image_surfaces)

    def test_using_current_render_target_as_image_sets_image_in_use(self):
        image=self.image();surface=self.bind(image);self.current(surface)
        before=bytes(self.b.surfaces[surface].buffer);self.driver.call('vgDrawImage',[image])
        self.assertEqual(self.b.error,0x1006);self.assertEqual(self.b.draw_count,0)
        self.assertEqual(bytes(self.b.surfaces[surface].buffer),before)

    def test_unknown_attributes_and_duplicate_binding_do_not_create_surfaces(self):
        image=self.image();self.bind(image);before=set(self.b.surfaces)
        with self.assertRaises(UnsupportedVG):self.bind(image)
        self.emu.write_mem(0x300,struct.pack('<3I',0x3057,32,0x3038))
        with self.assertRaises(UnsupportedVG):
            self.driver.call('eglCreatePbufferFromClientBuffer',[self.driver.display,0x3096,image,self.driver.config,0x300])
        self.assertEqual(set(self.b.surfaces),before)

    def test_original_dmp_pitch_aligns_width_but_preserves_eight_pixel_exception(self):
        for width,height,storage_width,storage_height in ((10,56,16,56),(8,3,8,8),(2,2,16,8)):
            row=bytes([255,0,0,255])*width+bytes([255,255,0,0])*(storage_width-width)
            raw=row*height+bytes(storage_width*4*(storage_height-height))
            self.emu.write_mem(0x1000,raw)
            image=self.driver.call('vgCreateImageStandardDataDMP',[2,width,height,len(raw),0x1000,1])
            self.assertEqual(set(self.b.image_pixels(image).getdata()),{(255,0,0,255)})
            self.assertEqual(self.b.image_pixels(image).size,(width,height))
        with self.assertRaises(ValueError):
            self.driver.call('vgCreateImageStandardDataDMP',[2,10,56,10*56*4,0x1000,1])

if __name__=='__main__':unittest.main()
