#!/usr/bin/env python3
"""Synthetic semantic/ABI tests, never represented as captured camera content."""
import copy
import tempfile
import unittest
import struct
from pathlib import Path
from graphics_hle import GraphicsHLE, word_float
from openvg_backend import IDENTITY, OpenVGBackend, UnsupportedVG, path_operations
from test_graphics_hle import Memory

class TransformPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='s5-transform-test-')
        self.emu=Memory();self.driver=GraphicsHLE(self.emu,self.tmp.name);self.b=self.driver.backend
    def tearDown(self):
        self.b.close();self.tmp.cleanup()
    def path(self,segments,coords,**kwargs):
        h=self.b.create_path(**kwargs);self.b.paths[h]['segments']=list(segments);self.b.paths[h]['coords']=list(coords);return h
    def ops(self,h):
        p=self.b.paths[h];return path_operations(p['segments'],p['coords'],p['scale'],p['bias'])
    def test_uses_path_matrix_not_selected_paint_matrix_and_preserves_source(self):
        s=self.path([2,7,9,11,15,13,17,0],[1,2,3,4,1,0,2,2,1,1,1,0,2,1,3,2,2,1,3,2])
        before=copy.deepcopy(self.b.paths[s]);d=self.path([2],[99,100])
        self.b.state[0x1100]=0x1402
        self.b.matrices[0x1402]=[99,0,0,0,99,0,1000,1000,1]
        self.b.matrices[0x1400]=[2,1,0,-1,3,0,5,-7,1]
        expected=[]
        for op in self.ops(s):
            v=[]
            for x,y in zip(op[1::2],op[2::2]):v.extend([2*x-y+5,x+3*y-7])
            expected.append((op[0],*v))
        self.b.transform_path(d,s)
        self.assertEqual(self.ops(d),[('M',99.,100.)]+expected)
        self.assertEqual(self.b.paths[s],before)
        self.assertEqual(self.b.state[0x1100],0x1402)
    def test_self_append_only_copies_original_snapshot(self):
        p=self.path([2,4,0],[1,2,4,5]);self.b.matrices[0x1400]=[1,0,0,0,1,0,10,20,1]
        self.b.transform_path(p,p)
        self.assertEqual(self.ops(p),[('M',1.,2.),('L',4.,5.),('Z',),('M',11.,22.),('L',14.,25.),('Z',)])
    def test_source_scale_bias_and_destination_inverse_encoding(self):
        s=self.path([2,4],[1,2,3,4],datatype=1,scale=2.,bias=10.)
        d=self.path([],[],scale=2.,bias=-4.)
        self.b.matrices[0x1400]=[2,0,0,0,3,0,1,2,1]
        self.b.transform_path(d,s)
        self.assertEqual(self.b.paths[d]['coords'],[14.5,24.,18.5,30.])
        self.assertEqual(self.ops(d),[('M',25.,44.),('L',33.,56.)])
    def test_caps_missing_sets_original_error_without_mutation(self):
        s=self.path([2],[1,2],capabilities=0);d=self.path([],[])
        before=copy.deepcopy(self.b.paths[d]);self.b.transform_path(d,s)
        self.assertEqual(self.b.error,0x1003);self.assertEqual(self.b.paths[d],before)
        self.b.paths[s]['capabilities']=8;self.b.paths[d]['capabilities']=0
        self.b.transform_path(d,s);self.assertEqual(self.b.error,0x1003)
    def test_unimplemented_arc_or_integer_destination_stops_without_mutation(self):
        s=self.path([2,18],[1,2,5,5,0,7,8]);d=self.path([],[])
        before=copy.deepcopy(self.b.paths[d])
        with self.assertRaises(UnsupportedVG):self.b.transform_path(d,s)
        self.assertEqual(self.b.paths[d],before)
        self.b.paths[s]['segments']=[2];self.b.paths[s]['coords']=[1,2]
        self.b.paths[d]['datatype']=1
        with self.assertRaises(UnsupportedVG):self.b.transform_path(d,s)
        self.assertEqual(self.b.paths[d]['segments'],[])
    def test_observed_entry_abi_uses_only_two_handles_and_returns_via_lr(self):
        s=self.path([2,4,0],[0,0,1000,1000]);d=self.path([],[])
        self.b.matrices[0x1400]=[.036,0,0,0,.036,0,0,0,1]
        regs={'r%d'%i:0 for i in range(16)};regs.update(r0=d,r1=s,r2=0xdeadbeef,r3=36,r13=0x100,r14=0x201,r15=0x65d9c64)
        result=self.driver.handle(0x65d9c64,regs)
        self.assertEqual(result['api'],'vgTransformPath');self.assertEqual(self.emu.regs[15],0x200)
        self.assertEqual(self.ops(d),[('M',0.,0.),('L',36.,36.),('Z',)])
        captures=list(Path(self.tmp.name).glob('transform-path-*.json'));self.assertEqual(len(captures),1)
    def test_create_and_clear_preserve_caller_capabilities(self):
        args=[0,3,word_float(1.),word_float(0.),0,0,8]
        p=self.driver.call('vgCreatePath',args);self.assertEqual(self.b.paths[p]['capabilities'],8)
        self.driver.call('vgClearPath',[p,16]);self.assertEqual(self.b.paths[p]['capabilities'],16)
    def test_scaled_path_renders_actual_transformed_geometry(self):
        surf=self.b.create_surface(32,32,'synthetic geometry test');self.b.make_current(surf)
        paint=self.b.create_paint();self.b.set_paint(paint,2);self.b.paints[paint][0x1a01]=[1,0,0,1]
        s=self.path([2,4,4,4,0],[0,0,100,0,100,100,0,100]);d=self.path([],[])
        self.b.matrices[0x1400]=[.1,0,0,0,.1,0,5,6,1];self.b.transform_path(d,s)
        self.b.matrices[0x1400]=IDENTITY.copy();self.driver.call('vgDrawPath',[d,2])
        image=self.b.surfaces[surf].image()
        self.assertEqual(image.getpixel((10,21)),(255,0,0,255))
        self.assertEqual(image.getpixel((20,21)),(0,0,0,0))

    def test_destroyed_paint_binding_retains_pixels_but_public_handle_is_invalid(self):
        surf=self.b.create_surface(20,20,'synthetic retained-paint test');self.b.make_current(surf)
        paint=self.b.create_paint();self.b.set_paint(paint,3)
        self.b.paints[paint][0x1a01]=[1,0,0,1]
        self.driver.call('vgDestroyPaint',[paint])
        self.assertNotIn(paint,self.b.paints);self.assertIn(paint,self.b.retired_paints)
        self.assertEqual(self.driver.call('vgGetPaint',[2]),0)
        self.b.draw_path_data([2,4,4,4,0],[2,2,12,2,12,12,2,12],2)
        self.assertEqual(self.b.surfaces[surf].image().getpixel((7,12)),(255,0,0,255))
        self.driver.call('vgSetColor',[paint,0x00ff00ff]);self.assertEqual(self.b.error,0x1000)
        self.assertEqual(self.b.retired_paints[paint][0x1a01],[1,0,0,1])
        self.b.set_paint(paint,2);self.assertEqual(self.b.error,0x1000)
        self.b.set_paint(0,2);self.assertIn(paint,self.b.retired_paints)
        self.assertEqual(self.b.color(2),[0.,0.,0.,1.])
        self.assertEqual(self.b.color(1),[1.,0.,0.,1.])
        self.b.set_paint(0,1);self.assertNotIn(paint,self.b.retired_paints)
        self.assertEqual(self.b.color(1),[0.,0.,0.,1.])

    def test_destroyed_bound_gradient_preserves_gradient_and_rebind_releases(self):
        old=self.b.create_paint();new=self.b.create_paint();self.b.set_paint(old,3)
        self.b.set_paint_parameter(old,0x1a00,[0x1b01]);self.b.set_paint_parameter(old,0x1a04,[0,0,20,0])
        self.b.destroy_paint(old)
        self.assertEqual(self.b.linear_gradient(2)['points'],[0,0,20,0])
        self.b.set_paint(new,3);self.assertNotIn(old,self.b.retired_paints)
        self.assertEqual(self.b.get_paint(2),new)
        self.b.destroy_paint(old);self.assertEqual(self.b.error,0x1000)

    def test_invalid_modes_leave_both_paint_bindings_unchanged(self):
        paint=self.b.create_paint();self.b.set_paint(paint,3);before=self.b.bound_paints.copy()
        self.b.set_paint(0,4);self.assertEqual(self.b.bound_paints,before);self.assertEqual(self.b.error,0x1001)

    def test_remove_caps_and_actual_paint_float_query(self):
        p=self.path([2],[1,2],capabilities=0xfff)
        self.driver.call('vgRemovePathCapabilities',[p,2]);self.assertEqual(self.b.paths[p]['capabilities'],0xffd)
        paint=self.b.create_paint();self.b.paints[paint][0x1a01]=[.25,.5,.75,1.]
        self.driver.call('vgGetParameterfv',[paint,0x1a01,4,0x300])
        self.assertEqual(struct.unpack('<4f',self.emu.read_mem(0x300,16)),(.25,.5,.75,1.))
        self.b.set_paint(paint,2);self.b.destroy_paint(paint)
        before=self.emu.read_mem(0x300,16)
        self.driver.call('vgGetParameterfv',[paint,0x1a01,4,0x300])
        self.assertEqual(self.b.error,0x1000);self.assertEqual(self.emu.read_mem(0x300,16),before)

    def frame_query_context(self):
        self.driver.call('eglInitialize',[self.driver.display,0,0])
        context=self.driver.call('eglCreateContext',[self.driver.display,self.driver.config,0,0])
        surface=self.b.create_surface(16,16,'synthetic frame-status ABI test')
        self.driver.call('eglMakeCurrent',[self.driver.display,surface,surface,context])
        return surface

    def test_frame_query_exact_word_abi_and_per_surface_read_clear(self):
        surface=self.frame_query_context();other=self.b.create_surface(8,8,'synthetic second surface')
        self.b.surfaces[surface].frame_retry_status=1
        self.b.surfaces[other].frame_retry_status=1
        self.emu.write_mem(0x300,bytes.fromhex('aaaaaaaa bbbbbbbb cccccccc'))
        regs={'r%d'%i:0 for i in range(16)}
        regs.update(r0=self.driver.display,r1=0x3fff,r2=0x304,r3=0xdeadbeef,
                    r13=0x100,r14=0x201,r15=0x64c4362)
        event=self.driver.handle(0x64c4362,regs)
        self.assertEqual(event['result'],1);self.assertEqual(self.emu.regs[15],0x200)
        self.assertEqual(self.emu.read_mem(0x300,12),bytes.fromhex('aaaaaaaa 01000000 cccccccc'))
        self.assertEqual(self.b.surfaces[surface].frame_retry_status,0)
        self.assertEqual(self.b.surfaces[other].frame_retry_status,1)
        self.driver.call('eglQueryFrameDMP',[self.driver.display,0x3fff,0x304])
        self.assertEqual(self.emu.read_mem(0x304,4),bytes(4))

    def test_frame_query_rejects_unknown_contract_without_clearing_or_output(self):
        surface=self.frame_query_context();self.b.surfaces[surface].frame_retry_status=1
        self.emu.write_mem(0x304,b'keep')
        for args in ([0,0x3fff,0x304],[self.driver.display,0x4000,0x304],
                     [self.driver.display,0x3fff,0],[self.driver.display,0x3fff,0x305]):
            with self.assertRaises((UnsupportedVG,ValueError)):
                self.driver.call('eglQueryFrameDMP',args)
            self.assertEqual(self.emu.read_mem(0x304,4),b'keep')
            self.assertEqual(self.b.surfaces[surface].frame_retry_status,1)
        self.driver.current_context=0
        with self.assertRaises(UnsupportedVG):self.driver.call('eglQueryFrameDMP',[self.driver.display,0x3fff,0x304])

    def test_software_draw_does_not_report_gpu_recovery(self):
        surface=self.frame_query_context()
        self.b.draw_path_data([2,4,4,0],[1,1,10,1,10,10],2)
        self.driver.call('vgFinish',[])
        self.driver.call('eglQueryFrameDMP',[self.driver.display,0x3fff,0x304])
        self.assertEqual(self.emu.read_mem(0x304,4),bytes(4))
        self.assertEqual(self.b.draw_count,1)

    def test_frame_modes_are_preserved_and_unknown_fault_filter_stops(self):
        surface=self.frame_query_context();canvas=self.b.surfaces[surface]
        self.assertEqual(self.driver.breakpoints[0x64c42b8],'__egl_frame_mode_control__')
        self.driver.call('__egl_frame_mode_control__',[self.driver.display,3])
        self.assertEqual(canvas.frame_mode,3)
        self.b.draw_path_data([2,4,4,0],[1,1,10,1,10,10],2)
        self.assertEqual(self.b.draw_count,1)
        self.driver.call('__egl_frame_mode_control__',[self.driver.display,0])
        self.assertEqual(canvas.frame_mode,0)
        with self.assertRaises(UnsupportedVG):self.driver.call('__egl_frame_mode_control__',[self.driver.display,2])
        self.assertEqual(canvas.frame_mode,0)
        canvas.frame_retry_status=1
        with self.assertRaises(UnsupportedVG):self.driver.call('__egl_frame_mode_control__',[self.driver.display,3])
        self.assertEqual(canvas.frame_retry_status,1)
        self.assertEqual(canvas.frame_mode,0)

if __name__=='__main__':unittest.main()
