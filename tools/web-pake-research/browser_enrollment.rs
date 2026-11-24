// SPDX-License-Identifier: GPL-3.0-or-later
//! Checkout-only enrollment slot. Separate from the existing login artifact.

#[path = "ocbox_profile/mod.rs"]
mod profile;

use opaque_ke::{
    rand::rand_core::{Infallible, TryCryptoRng, TryRng},
    ClientLogin, ClientLoginFinishParameters, ClientRegistration, ClientRegistrationFinishParameters,
    CredentialResponse, Identifiers, RegistrationResponse,
};
use profile::Suite;
use std::cell::RefCell;
use zeroize::{Zeroize, Zeroizing};

const IO_SIZE: usize = 1024;
const RESPONSE: usize = 64;
const ID: usize = 32;
const UPLOAD: usize = 192;
const KE1: usize = 96;
const KE2: usize = 320;
const CONTEXT: usize = 32;

#[link(wasm_import_module = "entropy")]
unsafe extern "C" {
    // The host fills the requested slice with secure bytes or traps.
    fn fill(ptr: *mut u8, len: usize);
}

struct BrowserEntropy;

impl TryRng for BrowserEntropy {
    type Error = Infallible;

    fn try_fill_bytes(&mut self, out: &mut [u8]) -> Result<(), Infallible> {
        unsafe { fill(out.as_mut_ptr(), out.len()) };
        Ok(())
    }

    fn try_next_u32(&mut self) -> Result<u32, Infallible> {
        let mut bytes = [0; 4];
        self.try_fill_bytes(&mut bytes)?;
        Ok(u32::from_le_bytes(bytes))
    }

    fn try_next_u64(&mut self) -> Result<u64, Infallible> {
        let mut bytes = [0; 8];
        self.try_fill_bytes(&mut bytes)?;
        Ok(u64::from_le_bytes(bytes))
    }
}

impl TryCryptoRng for BrowserEntropy {}

struct Password {
    bytes: Zeroizing<[u8; IO_SIZE]>,
    size: usize,
}

impl Password {
    fn value(&self) -> &[u8] {
        &self.bytes[..self.size]
    }
}

struct BoundIds {
    client: [u8; ID],
    server: [u8; ID],
}

impl BoundIds {
    fn value(&self) -> Identifiers<'_> {
        Identifiers { client: Some(&self.client), server: Some(&self.server) }
    }
}

enum State {
    Fresh,
    Registering(ClientRegistration<Suite>, Password),
    Proving(ClientLogin<Suite>, Password, BoundIds),
    Closed,
}

struct Slot {
    io: [u8; IO_SIZE],
    state: State,
}

impl Slot {
    fn registration_start(&mut self, size: usize) -> Option<usize> {
        let old = std::mem::replace(&mut self.state, State::Closed);
        if !matches!(old, State::Fresh) || !(1..=IO_SIZE).contains(&size) {
            self.io.zeroize();
            return None;
        }
        let mut password = Password { bytes: Zeroizing::new([0; IO_SIZE]), size };
        password.bytes[..size].copy_from_slice(&self.io[..size]);
        self.io.zeroize();
        let current = ClientRegistration::<Suite>::start(&mut BrowserEntropy, password.value()).ok()?;
        let request = current.message.serialize();
        if request.len() != 32 {
            return None;
        }
        self.io[..32].copy_from_slice(request.as_slice());
        self.state = State::Registering(current.state, password);
        Some(32)
    }

    fn registration_finish(&mut self) -> Option<usize> {
        let old = std::mem::replace(&mut self.state, State::Closed);
        let mut frame = [0; RESPONSE + 2 * ID];
        frame.copy_from_slice(&self.io[..RESPONSE + 2 * ID]);
        self.io.zeroize();
        let State::Registering(registration, password) = old else { return None };
        let response = RegistrationResponse::deserialize(&frame[..RESPONSE]).ok()?;
        let ids = BoundIds {
            client: frame[RESPONSE..RESPONSE + ID].try_into().ok()?,
            server: frame[RESPONSE + ID..].try_into().ok()?,
        };
        let mut completed = registration.finish(
            &mut BrowserEntropy, password.value(), response,
            ClientRegistrationFinishParameters::new(ids.value(), None),
        ).ok()?;
        completed.export_key.zeroize();
        let upload = Zeroizing::new(completed.message.serialize());
        let login = ClientLogin::<Suite>::start(&mut BrowserEntropy, password.value()).ok()?;
        let ke1 = login.message.serialize();
        if upload.len() != UPLOAD || ke1.len() != KE1 {
            return None;
        }
        self.io[..UPLOAD].copy_from_slice(upload.as_slice());
        self.io[UPLOAD..UPLOAD + KE1].copy_from_slice(ke1.as_slice());
        // Fresh login state, but the same locally entered password and the
        // exact client/server identifiers used to make the upload.
        self.state = State::Proving(login.state, password, ids);
        Some(UPLOAD + KE1)
    }

    fn proof_finish(&mut self) -> Option<usize> {
        let old = std::mem::replace(&mut self.state, State::Closed);
        let mut frame = [0; KE2 + CONTEXT];
        frame.copy_from_slice(&self.io[..KE2 + CONTEXT]);
        self.io.zeroize();
        let State::Proving(login, password, ids) = old else { return None };
        let response = CredentialResponse::deserialize(&frame[..KE2]).ok()?;
        let mut verified = login.finish(
            &mut BrowserEntropy, password.value(), response,
            ClientLoginFinishParameters::new(Some(&frame[KE2..]), ids.value(), None),
        ).ok()?;
        verified.session_key.zeroize();
        verified.export_key.zeroize();
        let proof = verified.message.serialize();
        if proof.len() != 64 {
            return None;
        }
        self.io[..64].copy_from_slice(proof.as_slice());
        Some(64)
    }
}

thread_local! {
    static SLOT: RefCell<Slot> = const { RefCell::new(Slot {
        io: [0; IO_SIZE], state: State::Fresh,
    }) };
}

#[unsafe(no_mangle)]
pub extern "C" fn io_addr() -> *mut u8 {
    SLOT.with(|slot| slot.borrow_mut().io.as_mut_ptr())
}

#[unsafe(no_mangle)]
pub extern "C" fn io_size() -> usize {
    IO_SIZE
}

#[unsafe(no_mangle)]
pub extern "C" fn registration_start(size: usize) -> i32 {
    SLOT.with(|slot| slot.borrow_mut().registration_start(size).map_or(-1, |n| n as i32))
}

#[unsafe(no_mangle)]
pub extern "C" fn registration_finish() -> i32 {
    SLOT.with(|slot| slot.borrow_mut().registration_finish().map_or(-1, |n| n as i32))
}

#[unsafe(no_mangle)]
pub extern "C" fn proof_finish() -> i32 {
    SLOT.with(|slot| slot.borrow_mut().proof_finish().map_or(-1, |n| n as i32))
}

#[unsafe(no_mangle)]
pub extern "C" fn discard() -> i32 {
    SLOT.with(|slot| {
        let mut slot = slot.borrow_mut();
        slot.state = State::Closed;
        slot.io.zeroize();
        1
    })
}

fn main() {}
