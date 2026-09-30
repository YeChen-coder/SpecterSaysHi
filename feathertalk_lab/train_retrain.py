"""Bounded-memory continuation with an entire clip withheld from training."""
import json, random, time, sys
from pathlib import Path
import cv2, numpy as np, torch
from model import Model
from train import compute_total_loss, save_checkpoint, mouth_l1_loss
from face_utils import mask_mouth, load_face_crop, extract_inner

torch.set_num_threads(4); torch.manual_seed(42); np.random.seed(42); random.seed(42)
torch.cuda.set_per_process_memory_fraction(.30); torch.backends.cudnn.benchmark=False
device=torch.device('cuda'); root=Path('/work/data/retrain_20260929')
out=Path('/work/output/retrain_20260929'); checkpoints=Path('/work/checkpoints/retrain_20260929')
checkpoints.mkdir(exist_ok=True); started=time.perf_counter()
manifest=json.loads((root/'manifest.json').read_text()); arrays={}; ranges={}
for split in ['train','validation']:
    crops=[]; masks=[]; feats=[]; starts=[]; ends=[]; count=0; pairs=[]
    for row in manifest['clips']:
        if row['split']!=split: continue
        path=root/row['name']; n=row['frames']
        crops.append(np.load(path/'crops.npy')); masks.append(np.load(path/'masks.npy')); feats.append(np.load(path/'features.npy'))
        starts.extend([count]*n); ends.extend([count+n]*n); pairs.extend(range(count,count+n-1)); count+=n
    # Crop cache remains uint8 in system RAM; only a batch moves to CUDA.
    gt=torch.from_numpy(np.concatenate(crops).transpose(0,3,1,2).copy())
    mk=torch.from_numpy(np.concatenate(masks))
    af=torch.from_numpy(np.concatenate(feats)).to(device)
    ids=torch.arange(count,device=device)[:,None]+torch.arange(-10,10,device=device)[None,:]
    valid=(ids>=torch.tensor(starts,device=device)[:,None])&(ids<torch.tensor(ends,device=device)[:,None])
    arrays[split]=(gt,mk,af,ids.clamp(0,count-1),valid)
    ranges[split]=np.asarray(pairs)

# Match OpenCV's exact rectangle convention, including its last pixel.
keep=torch.from_numpy(mask_mouth(np.ones((144,144,3),np.uint8)).transpose(2,0,1).astype(np.float32)).to(device)
base=extract_inner(load_face_crop('/work/data/base.jpg','/work/data/base.lms'))
base=torch.from_numpy(base.transpose(2,0,1).copy()).float().to(device)/255
net=Model().to(device); opt=torch.optim.Adam(net.parameters(),lr=.0002)
resume=sys.argv[2] if len(sys.argv)>2 else '/work/checkpoints/epoch_100.pth'
cp=torch.load(resume,map_location=device)
net.load_state_dict(cp['model']); opt.load_state_dict(cp['optimizer']); start=cp['epoch']+1
for group in opt.param_groups: group['lr']=.0002
scaler=torch.cuda.amp.GradScaler(); batch=int(sys.argv[3]) if len(sys.argv)>3 else 8
extra=int(sys.argv[1]) if len(sys.argv)>1 else 40

def get_batch(split, ids):
    gt,mk,af,windows,valid=arrays[split]
    cpu=torch.as_tensor(ids,dtype=torch.long).flatten()
    target=gt[cpu].to(device).float()/255; masks=mk[cpu].to(device).float()
    gpu=cpu.to(device)
    audio=(af[windows[gpu]]*valid[gpu,:,None,None]).reshape(-1,40,1024)
    return target,masks,audio

@torch.inference_mode()
def validate():
    net.eval(); n=len(arrays['validation'][0]); nums=[]; dens=[]; pixels=[]
    for b in range(0,n,8):
        target,masks,audio=get_batch('validation',np.arange(b,min(b+8,n)))
        inp=torch.cat([base[None].expand(len(target),-1,-1,-1),target*keep],1)
        pred=net(inp,audio)
        nums.append(float(((pred-target).abs()*masks).sum())); dens.append(float(masks.sum()*3))
        pixels.append(float((pred-target).abs().sum()))
    return dict(mouth_l1=sum(nums)/sum(dens),pixel_l1=sum(pixels)/(n*3*144*144))

prior={}
if resume!='/work/checkpoints/epoch_100.pth':
    prior=json.loads((out/'training_metrics.json').read_text())
baseline=prior.get('baseline') or validate()
best=prior.get('best_validation_mouth_l1',baseline['mouth_l1'])
history=[row for row in prior.get('history',[]) if row['epoch']<=start]
print(json.dumps(dict(event='ready',train_frames=len(arrays['train'][0]),validation_frames=len(arrays['validation'][0]),baseline=baseline,batch_pairs=batch)),flush=True)
if not prior: save_checkpoint(str(checkpoints/'best.pth'),net,opt,start-1)
best_epoch=prior.get('best_epoch',start)
prior_phases=prior.get('phases',[])
if prior and not prior_phases:
    prior_phases=[dict(start_epoch=prior['start_epoch'],batch_pairs=prior['batch_pairs'],requested_extra_epochs=start-prior['start_epoch'],resume='/work/checkpoints/epoch_100.pth')]
phases=prior_phases+[dict(start_epoch=start,batch_pairs=batch,requested_extra_epochs=extra,resume=resume)]
stale=0; patience_reference=best
for epoch in range(start,start+extra):
    net.train(); begin=time.perf_counter(); order=np.random.permutation(ranges['train']); losses=[]; skipped=0
    for b in range(0,len(order),batch):
        ids=order[b:b+batch]; pair=np.stack([ids,ids+1],axis=1)
        target,masks,audio=get_batch('train',pair)
        refs=np.random.randint(len(arrays['train'][0]),size=len(ids))
        reference=arrays['train'][0][torch.from_numpy(refs)].to(device).float()/255
        reference=reference[:,None].expand(-1,2,-1,-1,-1).flatten(0,1)
        inp=torch.cat([reference,target*keep],1)
        with torch.autocast(device_type='cuda',dtype=torch.float16):
            pred=net(inp,audio).reshape(len(ids),2,3,144,144)
        # Reductions in float32 avoid overflow over tens of thousands of pixels.
        loss,parts=compute_total_loss(pred.float(),target.reshape_as(pred),masks.reshape(len(ids),2,1,144,144),torch.nn.L1Loss(),lambda x,y:x.new_zeros(()),4.,.5,4.,0.,.5,5)
        if not torch.isfinite(loss): raise RuntimeError('Nonfinite training loss')
        opt.zero_grad(set_to_none=True); before=scaler.get_scale()
        scaler.scale(loss).backward(); scaler.step(opt); scaler.update()
        skipped+=int(scaler.get_scale()<before); losses.append(parts)
        if epoch==start and b in (0,400,1600):
            print(json.dumps(dict(event='batch_progress',epoch=epoch+1,pairs_done=min(b+batch,len(order)),seconds=time.perf_counter()-begin,cuda_allocated_mb=torch.cuda.memory_allocated()/2**20,cuda_reserved_mb=torch.cuda.memory_reserved()/2**20)),flush=True)
    torch.cuda.synchronize()
    row=dict(epoch=epoch+1,batch_pairs=batch,seconds=time.perf_counter()-begin,amp_skipped_steps=skipped,**{k:float(np.mean([p[k] for p in losses])) for k in losses[0]})
    if (epoch+1)%5==0 or epoch+1==start+extra:
        row['validation']=validate()
        if row['validation']['mouth_l1']<patience_reference*.99:
            patience_reference=row['validation']['mouth_l1']; stale=0
        else: stale+=1
        if row['validation']['mouth_l1']<best:
            best=row['validation']['mouth_l1']; best_epoch=epoch+1
            save_checkpoint(str(checkpoints/'best.pth'),net,opt,epoch)
        save_checkpoint(str(checkpoints/'last.pth'),net,opt,epoch)
    history.append(row); print(json.dumps(row),flush=True)
    report=dict(train_frames=len(arrays['train'][0]),validation_frames=len(arrays['validation'][0]),validation_clip='english_training_20260929_11',start_epoch=prior.get('start_epoch',start),baseline=baseline,best_epoch=best_epoch,best_validation_mouth_l1=best,learning_rate=.0002,batch_pairs=batch,amp=True,phases=phases,history=history,elapsed_seconds=prior.get('elapsed_seconds',0)+time.perf_counter()-started,peak_cuda_mb=max(prior.get('peak_cuda_mb',0),torch.cuda.max_memory_allocated()/2**20),training_complete=False)
    (out/'training_metrics.json').write_text(json.dumps(report,indent=2))
    if stale>=3 and epoch+1>=120:
        print('EARLY_STOP: three validation checks without 1% relative improvement',flush=True); break
save_checkpoint(str(checkpoints/f'epoch_{epoch+1}.pth'),net,opt,epoch)
report['training_complete']=True
report['stop_reason']='validation_plateau' if stale>=3 else 'requested_epochs_completed'
(out/'training_metrics.json').write_text(json.dumps(report,indent=2))
print('TRAINING_COMPLETE',json.dumps(report | {'history':'see training_metrics.json'}),flush=True)
