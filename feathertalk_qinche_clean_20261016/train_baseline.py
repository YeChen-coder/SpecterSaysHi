"""Gentleman initial recipe: random Model, 60 + 40 epochs, original losses."""
import json,random,time,hashlib
from pathlib import Path
import numpy as np,torch
from model import Model
from train import compute_total_loss,save_checkpoint
from face_utils import mask_mouth,load_face_crop,extract_inner

torch.set_num_threads(4);torch.manual_seed(42);np.random.seed(42);random.seed(42)
torch.backends.cudnn.benchmark=True;torch.cuda.set_per_process_memory_fraction(.45)
device=torch.device('cuda');root=Path('/work/data');out=Path('/work/output');checkpoints=Path('/work/checkpoints');checkpoints.mkdir(exist_ok=True)
manifest=json.loads((root/'manifest.json').read_text());arrays={};pairs={}
for split in ['train','validation']:
    crops=[];masks=[];features=[];starts=[];ends=[];pair=[];n=0
    for row in manifest['clips']:
        if row['split']!=split:continue
        p=root/row['name'];count=row['frames'];crops.append(np.load(p/'crops.npy'));masks.append(np.load(p/'masks.npy'));features.append(np.load(p/'features.npy'))
        starts.extend([n]*count);ends.extend([n+count]*count);pair.extend(range(n,n+count-1));n+=count
    gt=torch.from_numpy(np.concatenate(crops).transpose(0,3,1,2).copy())
    mk=torch.from_numpy(np.concatenate(masks));af=torch.from_numpy(np.concatenate(features)).to(device)
    indices=torch.arange(n,device=device)[:,None]+torch.arange(-10,10,device=device)[None,:]
    valid=(indices>=torch.tensor(starts,device=device)[:,None])&(indices<torch.tensor(ends,device=device)[:,None])
    arrays[split]=(gt,mk,af,indices.clamp(0,n-1),valid);pairs[split]=np.asarray(pair)
keep=torch.from_numpy(mask_mouth(np.ones((144,144,3),np.uint8)).transpose(2,0,1).astype(np.float32)).to(device)
base=torch.from_numpy(extract_inner(load_face_crop(str(root/'base.jpg'),str(root/'base.lms'))).transpose(2,0,1).copy()).float().to(device)/255
net=Model().to(device);opt=torch.optim.Adam(net.parameters(),lr=.001)
# No torch.load, no old checkpoint, no AMP, no causal/warm-reference mixing.
initial=hashlib.sha256(b''.join(v.detach().cpu().numpy().tobytes() for v in net.state_dict().values())).hexdigest()
save_checkpoint(str(checkpoints/'initial_random.pth'),net,opt,-1)
recipe=dict(initialization='random Model; seed 42',initial_state_sha256=initial,batch_pairs=8,precision='float32',schedule=[dict(epochs=60,lr=.001),dict(epochs=40,lr=.0003)],loss=dict(full=1,mouth=4,temporal=.5,temporal_mouth=4,perceptual=0,mouth_gradient=.5,temporal_lowpass_kernel=5),audio='full-clip features, 20-frame centred windows, zero outside each clip',references='random training frame, same reference for adjacent pair',old_artifacts_used=False,notes='CPU uint8 cache and per-batch GPU transfer only change memory placement; held-out clips are evaluation only, not used for selection or early stopping.')
(out/'recipe.json').write_text(json.dumps(recipe,indent=2))
def batch(split,ids):
    gt,mk,af,windows,valid=arrays[split];cpu=torch.as_tensor(ids,dtype=torch.long).flatten();gpu=cpu.to(device)
    return gt[cpu].to(device).float()/255,mk[cpu].to(device).float(),(af[windows[gpu]]*valid[gpu,:,None,None]).reshape(-1,40,1024)
@torch.inference_mode()
def validate():
    net.eval();num=den=0.
    for i in range(0,len(arrays['validation'][0]),8):
        target,masks,audio=batch('validation',np.arange(i,min(i+8,len(arrays['validation'][0]))))
        pred=net(torch.cat([base[None].expand(len(target),-1,-1,-1),target*keep],1),audio)
        num+=float(((pred-target).abs()*masks).sum());den+=float(masks.sum()*3)
    return dict(mouth_l1=num/den)
history=[];started=time.perf_counter();report=dict(recipe=recipe,train_frames=len(arrays['train'][0]),validation_frames=len(arrays['validation'][0]),training_complete=False,history=history)
for epoch in range(100):
    if epoch==60:
        # The original Gentleman continuation ran in a new seeded process.
        torch.manual_seed(42);np.random.seed(42);random.seed(42)
    lr=.001 if epoch<60 else .0003
    for group in opt.param_groups:group['lr']=lr
    net.train();begin=time.perf_counter();order=np.random.permutation(pairs['train']);losses=[]
    for offset in range(0,len(order),8):
        ids=order[offset:offset+8];pair=np.stack([ids,ids+1],1);target,masks,audio=batch('train',pair)
        refs=torch.randint(len(arrays['train'][0]),(len(ids),),device=device).cpu();reference=arrays['train'][0][refs].to(device).float()/255
        reference=reference[:,None].expand(-1,2,-1,-1,-1).flatten(0,1)
        pred=net(torch.cat([reference,target*keep],1),audio).reshape(len(ids),2,3,144,144)
        loss,parts=compute_total_loss(pred,target.reshape_as(pred),masks.reshape(len(ids),2,1,144,144),torch.nn.L1Loss(),lambda x,y:x.new_zeros(()),4.,.5,4.,0.,.5,5)
        assert torch.isfinite(loss),'Nonfinite loss'
        opt.zero_grad(set_to_none=True);loss.backward();opt.step();losses.append(parts)
        if offset%800==0:
            (out/'training_progress.json').write_text(json.dumps(dict(epoch=epoch+1,epochs=100,pairs_done=min(offset+8,len(order)),pairs=len(order),learning_rate=lr,seconds=time.perf_counter()-begin)))
    torch.cuda.synchronize();row=dict(epoch=epoch+1,learning_rate=lr,seconds=time.perf_counter()-begin,**{k:float(np.mean([p[k] for p in losses])) for k in losses[0]})
    if (epoch+1)%10==0:
        row['validation']=validate();save_checkpoint(str(checkpoints/f'epoch_{epoch+1}.pth'),net,opt,epoch)
        save_checkpoint(str(checkpoints/'last.pth'),net,opt,epoch)
    history.append(row);report.update(epoch=epoch+1,elapsed_seconds=time.perf_counter()-started,peak_cuda_mb=torch.cuda.max_memory_allocated()/2**20)
    (out/'training_metrics.json').write_text(json.dumps(report,indent=2));print(json.dumps(row),flush=True)
report.update(training_complete=True,selected_epoch=100,selection='Fixed original Gentleman schedule; validation does not select checkpoints')
(out/'training_metrics.json').write_text(json.dumps(report,indent=2));print('TRAINING_COMPLETE',flush=True)
