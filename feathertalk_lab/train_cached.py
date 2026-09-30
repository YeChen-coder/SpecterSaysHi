import sys,time,json,random
from pathlib import Path
import numpy as np,torch
from model import Model
from dataset_mouth_roi import TemporalMouthRoiDataset,MouthRoiDataset
from train import compute_total_loss,save_checkpoint
torch.set_num_threads(4);torch.manual_seed(42);np.random.seed(42);random.seed(42)
torch.backends.cudnn.benchmark=True
torch.cuda.set_per_process_memory_fraction(0.45)
device=torch.device('cuda');t=time.perf_counter()
ds=TemporalMouthRoiDataset('/work/data/person');n=MouthRoiDataset.__len__(ds)
targets=[];masked=[];masks=[];audio=[]
for i in range(n):
    gt,ms,mk=ds._build_target_masked_and_mouth(i);targets.append(gt);masked.append(ms);masks.append(mk)
    audio.append(ds._build_frame(i,gt)[2])
gt=torch.stack(targets).to(device);ms=torch.stack(masked).to(device);mk=torch.stack(masks).to(device);af=torch.stack(audio).to(device)
net=Model().to(device);opt=torch.optim.Adam(net.parameters(),lr=0.001)
epochs=int(sys.argv[1]) if len(sys.argv)>1 else 60
batch=8;history=[];root=Path('/work/checkpoints');root.mkdir(exist_ok=True)
start=0
if len(sys.argv)>2:
    cp=torch.load(sys.argv[2],map_location=device);net.load_state_dict(cp['model']);opt.load_state_dict(cp['optimizer']);start=cp['epoch']+1
    for group in opt.param_groups:group['lr']=0.0003
print('cache ready',n,'frames',time.perf_counter()-t,flush=True)
for epoch in range(start,epochs):
    net.train();begin=time.perf_counter();order=np.random.permutation(ds.pair_starts);losses=[]
    for b in range(0,len(order),batch):
        ids=torch.as_tensor(order[b:b+batch],device=device);pair=torch.stack([ids,ids+1],1);refs=torch.randint(n,(len(ids),),device=device)
        inp=torch.cat([gt[refs][:,None].expand(-1,2,-1,-1,-1),ms[pair]],2)
        pred=net(inp.flatten(0,1),af[pair].flatten(0,1)).reshape(len(ids),2,3,144,144)
        loss,parts=compute_total_loss(pred,gt[pair],mk[pair],torch.nn.L1Loss(),lambda x,y: x.new_zeros(()),4.,0.5,4.,0.,0.5,5)
        opt.zero_grad(set_to_none=True);loss.backward();opt.step();losses.append(parts)
    torch.cuda.synchronize();row={'epoch':epoch+1,'seconds':time.perf_counter()-begin,**{k:float(np.mean([p[k] for p in losses])) for k in losses[0]}}
    history.append(row);print(json.dumps(row),flush=True)
    if (epoch+1)%20==0 or epoch+1==epochs:
        save_checkpoint(str(root/f'epoch_{epoch+1}.pth'),net,opt,epoch);save_checkpoint(str(root/'last.pth'),net,opt,epoch)
        report={'frames':n,'start_epoch':start,'elapsed_seconds':time.perf_counter()-t,'perceptual_weight':0,'history':history,'peak_cuda_mb':torch.cuda.max_memory_allocated()/2**20}
        Path('/work/output/training_metrics.json').write_text(json.dumps(report,indent=2))
        Path(f'/work/output/training_metrics_phase_{start}.json').write_text(json.dumps(report,indent=2))
