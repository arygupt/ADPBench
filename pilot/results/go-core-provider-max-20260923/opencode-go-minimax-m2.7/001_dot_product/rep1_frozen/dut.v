// ADPBench submission. Replace the body; keep the module name and ports.
// Run ./check.sh for synthesis + simulation feedback.
// The testbench drives several back-to-back transactions without reset, and
// every input port has its own valid/ready handshake.

module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam BEATS = LEN / LANES;  // 8 beats per transaction
    
    // State machine states
    typedef enum logic [1:0] {
        IDLE    = 2'b00,
        COMPUTE = 2'b01,
        OUTPUT  = 2'b10
    } state_t;
    
    state_t state, next_state;
    
    // Beat counters for each independent input stream
    logic [$clog2(BEATS)-1:0] a_cnt;
    logic [$clog2(BEATS)-1:0] b_cnt;
    
    // Accumulator for dot product result
    logic signed [ACC_W-1:0] acc;
    
    // Output result registered for output state
    logic signed [ACC_W-1:0] out_c_reg;
    
    // Ready signals - accept new beats only in IDLE
    assign in_a_flat_ready = (state == IDLE);
    assign in_b_flat_ready = (state == IDLE);
    
    // Output valid when in OUTPUT state
    assign out_valid = (state == OUTPUT);
    
    // Output result - registered in OUTPUT, combinational output
    assign out_c = (state == OUTPUT) ? acc : out_c_reg;
    
    // State transition logic
    always_comb begin
        case (state)
            IDLE:    next_state = (in_a_flat_valid && in_b_flat_valid) ? COMPUTE : IDLE;
            COMPUTE: next_state = (a_cnt == BEATS-1 && b_cnt == BEATS-1) ? OUTPUT : COMPUTE;
            OUTPUT:  next_state = out_ready ? IDLE : OUTPUT;
            default: next_state = IDLE;
        endcase
    end
    
    // State and datapath registers
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state   <= IDLE;
            a_cnt   <= 0;
            b_cnt   <= 0;
            acc     <= 0;
            out_c_reg <= 0;
        end else begin
            state <= next_state;
            
            case (next_state)
                IDLE: begin
                    // Clear per-transaction state
                    a_cnt <= 0;
                    b_cnt <= 0;
                    acc   <= 0;
                end
                
                COMPUTE: begin
                    // Accept new beat when both streams ready
                    if (in_a_flat_valid && in_a_flat_ready &&
                        in_b_flat_valid && in_b_flat_ready) begin
                        a_cnt <= a_cnt + 1;
                        b_cnt <= b_cnt + 1;
                        // Multiply-accumulate: 32 int8 products added to int32 accumulator
                        for (int i = 0; i < LANES; i++) begin
                            acc <= acc + $signed(in_a_flat[i*DATA_W +: DATA_W]) *
                                          $signed(in_b_flat[i*DATA_W +: DATA_W]);
                        end
                    end
                end
                
                OUTPUT: begin
                    // Hold result, clear state for next transaction
                    out_c_reg <= acc;
                    a_cnt <= 0;
                    b_cnt <= 0;
                    acc   <= 0;
                end
            endcase
        end
    end

endmodule
