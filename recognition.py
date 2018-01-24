from enumeration import *
from fragmentGrammar import *
from grammar import *
from utilities import eprint

import gc

import torch
import torch.nn as nn
import torch.optim as optimization
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.transforms as T

import numpy as np

def variable(x, volatile=False):
    if isinstance(x,list): x = np.array(x)
    if isinstance(x,(np.ndarray,np.generic)): x = torch.from_numpy(x)
    #if GPU: x = x.cuda()
    return Variable(x, volatile=volatile)

class RecognitionModel(nn.Module):
    def __init__(self, featureDimensionality, grammar, hidden=[5], activation="relu", cuda=False):
        super(RecognitionModel, self).__init__()
        self.grammar = grammar
        self.use_cuda = cuda
        if cuda:
            self.cuda()
        else:
            # Torch sometimes segfaults in multithreaded mode...
            torch.set_num_threads(1)


        self.hiddenLayers = []
        inputDimensionality = featureDimensionality
        for h in hidden:
            layer = nn.Linear(inputDimensionality, h)
            if cuda:
                layer = layer.cuda()
            self.hiddenLayers.append(layer)
            inputDimensionality = h

        if activation == "sigmoid":
            self.activation = F.sigmoid
        elif activation == "relu":
            self.activation = lambda x: x.clamp(min = 0)
        elif activation == "tanh":
            self.activation = F.tanh
        else:
            raise Exception('Unknown activation function '+str(activation))

        self.logVariable = nn.Linear(inputDimensionality,1)
        self.logProductions = nn.Linear(inputDimensionality, len(self.grammar))
        if cuda:
            self.logVariable = self.logVariable.cuda()
            self.logProductions = self.logProductions.cuda()

    def forward(self, features):
        for layer in self.hiddenLayers:
            features = self.activation(layer(features))
        h = features
        return self.logVariable(h), self.logProductions(h)

    def extractFeatures(self, tasks):
        fs = torch.from_numpy(np.array([ task.features for task in tasks ])).float()
        if self.use_cuda:
            fs = fs.cuda()
        return Variable(fs)
    
    def posteriorKL(self, frontiers, KLRegularize):
        features = self.extractFeatures([ frontier.task for frontier in frontiers ])
        variables, productions = self(features)
        kl = 0
        for j,frontier in enumerate(frontiers):
            v,p = variables[j],productions[j]
            g = Grammar(v, [(p[k],t,program) for k,(_,t,program) in enumerate(self.grammar.productions) ])
            for entry in frontier:
                kl -= math.exp(entry.logPosterior) * g.closedLogLikelihood(frontier.task.request, entry.program)
                      
            if KLRegularize:
                P = [self.grammar.logVariable] + [ l for l,_1,_2 in self.grammar.productions ]
                P = Variable(torch.from_numpy(np.array(P)), requires_grad = False).float()
                Q = torch.cat((variables[0], productions[0]))
                # Normalized distributions
                P = F.log_softmax(P,dim = 0)
                Q = F.log_softmax(Q,dim = 0)
                if True: # regularize KL(P||Q)
                    # This will force Q to spread its mass everywhere that P does
                    pass
                else: # regularize KL(Q||P)
                    # This will force q to hug one of the modes of p
                    P,Q = Q,P
                # torch built in F.kl_div won't let you regularize D(P||Q)
                # it cannot differentiate with respect to the target distribution
                # If torch was less ridiculous, you could just do this:
                # D = F.kl_div(P,Q)
                D = (P.exp() * (P - Q)).sum()
                kl += KLRegularize * D
        return kl

    def train(self, frontiers, _=None, KLRegularize=0.1, steps=500, lr=0.001, topK=1, CPUs=1):
        frontiers = [ frontier.topK(topK).normalize() for frontier in frontiers if not frontier.empty ]
        eprint("Training a recognition model from %d frontiers. KLRegularize = %s"%(len(frontiers),
                                                                                    KLRegularize))
        
        optimizer = torch.optim.Adam(self.parameters(), lr=lr)
        with timing("Trained recognition model"):
            for i in range(1,steps + 1):
                losses = []
                for batch in batches(frontiers):
                    self.zero_grad()
                    loss = self.posteriorKL(batch, KLRegularize)/len(batch)
                    loss.backward()
                    optimizer.step()
                    losses.append(loss.data[0])
                if i%50 == 0:
                    eprint("Epoch",i,"Loss",sum(losses)/len(losses))
                    gc.collect()
                

    def enumerateFrontiers(self, frontierSize, tasks,
                           CPUs=1, maximumFrontier=None):
        with timing("Evaluated recognition model"):
            features = self.extractFeatures(tasks)
            variables, productions = self(features)
            grammars = {task: Grammar(variables.data[j][0],
                                      [ (productions.data[j][k],t,p)
                                        for k,(_,t,p) in enumerate(self.grammar.productions) ])
                        for j,task in enumerate(tasks) }

        return callCompiled(enumerateFrontiers,
                            grammars, frontierSize, tasks,
                            CPUs = CPUs, maximumFrontier = maximumFrontier)

# class TreeDecoder(nn.Module):
#     def __init__(self, grammar, hiddenUnits = 10):
#         super(TreeDecoder, self).__init__()

#         self.ancestral = nn.LSTM(input_size = hiddenUnits, hidden_size = hiddenUnits, num_layers = 1,
#                                  batch_first = True)
#         self.fraternal = nn.LSTM(input_size = hiddenUnits, hidden_size = hiddenUnits, num_layers = 1,
#                                  batch_first = True)
        
#         self.grammar = grammar

#         # self.embedding : list of N indices (BxW) -> (B,W,EMBEDDINGSIZE)
#         self.embedding = nn.Embedding(len(grammar.productions) + 1, hiddenUnits)

#         self.primitiveToIndex = {p: j + 1
#                                  for j,(_,_,p) in enumerate(grammar.productions) }

#         self.tokenPrediction = nn.Linear(hiddenUnits, len(grammar.productions) + 1)

#         self.uf = nn.Linear(hiddenUnits, hiddenUnits)
#         self.ua = nn.Linear(hiddenUnits, hiddenUnits)

#     def embed(self, primitive):
#         if isinstance(primitive,Index): j = 0
#         else: j = self.primitiveToIndex[primitive]
#         j = variable([j]).unsqueeze(0)
#         return self.embedding(j)

#     def updateAncestralState(self, ancestralSymbol, previous = None):
#         ancestor = self.embed(ancestralSymbol)
#         return self.ancestral(ancestor, previous)

#     def updateSiblingState(self, siblingSymbol, previous = None):
#         sibling = self.embed(siblingSymbol)
#         return self.fraternal(sibling, previous)

#     def predictPrimitive(self, ancestralOutput, siblingOutput):
#         ancestralOutput, siblingOutput = ancestralOutput.squeeze(0), siblingOutput.squeeze(0)
#         a = self.ua(ancestralOutput)
#         s = self.uf(siblingOutput)
#         return F.log_softmax(self.tokenPrediction(F.tanh(a + s)))

#     def buildCandidates(self, context, environment, request):
#         candidates = []
#         for _,t,p in self.grammar.productions:
#             try:
#                 newContext, t = t.instantiate(context)
#                 newContext = newContext.unify(t.returns(), request)
#                 candidates.append((newContext,
#                                    t.apply(newContext),
#                                    p))
#             except UnificationFailure: continue
#         for j,t in enumerate(environment):
#             try:
#                 newContext = context.unify(t.returns(), request)
#                 candidates.append((newContext,
#                                    t.apply(newContext),
#                                    Index(j)))
#             except UnificationFailure: continue
#         return candidates

#     def logLikelihood(self, context, environment, request, expression, states = (None,None), parentSymbol = None):
#         request = request.apply(context)
        
#         if request.isArrow():
#             if not isinstance(expression,Abstraction):
#                 raise Exception('expected abstraction')
#             return self.logLikelihood(context,
#                                       [request.arguments[0]] + environment,
#                                       request.arguments[1],
#                                       expression.body,
#                                       states,
#                                       parentSymbol)

#         def applicationParse(e):
#             if isinstance(e,Application):
#                 f,xs = applicationParse(e.f)
#                 return f,xs + [e]
#             else: return e,[]
#         f,xs = applicationParse(expression)
        
#         candidates = self.buildCandidates(context, environment, request)
#         distribution = self.predictPrimitive(states[0], states[1])        
    

# if __name__ == "__main__":
#     from arithmeticPrimitives import *
#     g = Grammar.uniform([addition, multiplication, k0, k1])
#     m = TreeDecoder(g, hiddenUnits = 9)
#     print m.embed(addition)
#     print 
#     print m.updateAncestralState(k0)
#     print 
#     print m.updateAncestralState(k1)[0].squeeze(0)
#     print
#     print m.predictPrimitive(m.updateAncestralState(k0)[0],
#                              m.updateSiblingState(k1)[0])
        

        
        
